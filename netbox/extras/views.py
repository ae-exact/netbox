from django import template
from django.conf import settings
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View
from django_rq.queues import get_connection
from django_tables2 import RequestConfig
from rq import Worker

from dcim.models import DeviceRole, Platform, Region, Site
from tenancy.models import Tenant, TenantGroup
from utilities.forms import ConfirmationForm
from utilities.paginator import EnhancedPaginator
from utilities.utils import copy_safe_request, shallow_compare_dict
from utilities.views import (
    BulkDeleteView, BulkEditView, BulkImportView, ObjectView, ObjectDeleteView, ObjectEditView, ObjectListView,
    ContentTypePermissionRequiredMixin,
)
from virtualization.models import Cluster, ClusterGroup
from . import filters, forms, tables
from .choices import JobResultStatusChoices
from .models import ConfigContext, ImageAttachment, ObjectChange, JobResult, Tag
from .reports import get_report, get_reports, run_report
from .scripts import get_scripts, run_script


#
# Tags
#

class TagListView(ObjectListView):
    queryset = Tag.objects.annotate(
        items=Count('extras_taggeditem_items')
    ).order_by(*Tag._meta.ordering)
    filterset = filters.TagFilterSet
    filterset_form = forms.TagFilterForm
    table = tables.TagTable


class TagEditView(ObjectEditView):
    queryset = Tag.objects.all()
    model_form = forms.TagForm
    template_name = 'extras/tag_edit.html'


class TagDeleteView(ObjectDeleteView):
    queryset = Tag.objects.all()


class TagBulkImportView(BulkImportView):
    queryset = Tag.objects.all()
    model_form = forms.TagCSVForm
    table = tables.TagTable


class TagBulkEditView(BulkEditView):
    queryset = Tag.objects.annotate(
        items=Count('extras_taggeditem_items')
    ).order_by(*Tag._meta.ordering)
    table = tables.TagTable
    form = forms.TagBulkEditForm


class TagBulkDeleteView(BulkDeleteView):
    queryset = Tag.objects.annotate(
        items=Count('extras_taggeditem_items')
    ).order_by(*Tag._meta.ordering)
    table = tables.TagTable


#
# Config contexts
#

class ConfigContextListView(ObjectListView):
    queryset = ConfigContext.objects.all()
    filterset = filters.ConfigContextFilterSet
    filterset_form = forms.ConfigContextFilterForm
    table = tables.ConfigContextTable
    action_buttons = ('add',)


class ConfigContextView(ObjectView):
    queryset = ConfigContext.objects.all()

    def get(self, request, pk):
        # Extend queryset to prefetch related objects
        self.queryset = self.queryset.prefetch_related(
            Prefetch('regions', queryset=Region.objects.restrict(request.user)),
            Prefetch('sites', queryset=Site.objects.restrict(request.user)),
            Prefetch('roles', queryset=DeviceRole.objects.restrict(request.user)),
            Prefetch('platforms', queryset=Platform.objects.restrict(request.user)),
            Prefetch('clusters', queryset=Cluster.objects.restrict(request.user)),
            Prefetch('cluster_groups', queryset=ClusterGroup.objects.restrict(request.user)),
            Prefetch('tenants', queryset=Tenant.objects.restrict(request.user)),
            Prefetch('tenant_groups', queryset=TenantGroup.objects.restrict(request.user)),
        )

        configcontext = get_object_or_404(self.queryset, pk=pk)

        # Determine user's preferred output format
        if request.GET.get('format') in ['json', 'yaml']:
            format = request.GET.get('format')
            if request.user.is_authenticated:
                request.user.config.set('extras.configcontext.format', format, commit=True)
        elif request.user.is_authenticated:
            format = request.user.config.get('extras.configcontext.format', 'json')
        else:
            format = 'json'

        return render(request, 'extras/configcontext.html', {
            'configcontext': configcontext,
            'format': format,
        })


class ConfigContextEditView(ObjectEditView):
    queryset = ConfigContext.objects.all()
    model_form = forms.ConfigContextForm
    template_name = 'extras/configcontext_edit.html'


class ConfigContextBulkEditView(BulkEditView):
    queryset = ConfigContext.objects.all()
    filterset = filters.ConfigContextFilterSet
    table = tables.ConfigContextTable
    form = forms.ConfigContextBulkEditForm


class ConfigContextDeleteView(ObjectDeleteView):
    queryset = ConfigContext.objects.all()


class ConfigContextBulkDeleteView(BulkDeleteView):
    queryset = ConfigContext.objects.all()
    table = tables.ConfigContextTable


class ObjectConfigContextView(ObjectView):
    base_template = None

    def get(self, request, pk):

        obj = get_object_or_404(self.queryset, pk=pk)
        source_contexts = ConfigContext.objects.restrict(request.user, 'view').get_for_object(obj)
        model_name = self.queryset.model._meta.model_name

        # Determine user's preferred output format
        if request.GET.get('format') in ['json', 'yaml']:
            format = request.GET.get('format')
            if request.user.is_authenticated:
                request.user.config.set('extras.configcontext.format', format, commit=True)
        elif request.user.is_authenticated:
            format = request.user.config.get('extras.configcontext.format', 'json')
        else:
            format = 'json'

        return render(request, 'extras/object_configcontext.html', {
            model_name: obj,
            'obj': obj,
            'rendered_context': obj.get_config_context(),
            'source_contexts': source_contexts,
            'format': format,
            'base_template': self.base_template,
            'active_tab': 'config-context',
        })


#
# Change logging
#

class ObjectChangeListView(ObjectListView):
    queryset = ObjectChange.objects.prefetch_related('user', 'changed_object_type')
    filterset = filters.ObjectChangeFilterSet
    filterset_form = forms.ObjectChangeFilterForm
    table = tables.ObjectChangeTable
    template_name = 'extras/objectchange_list.html'
    action_buttons = ('export',)


class ObjectChangeView(ObjectView):
    queryset = ObjectChange.objects.all()

    def get(self, request, pk):

        objectchange = get_object_or_404(self.queryset, pk=pk)

        related_changes = ObjectChange.objects.restrict(request.user, 'view').filter(
            request_id=objectchange.request_id
        ).exclude(
            pk=objectchange.pk
        )
        related_changes_table = tables.ObjectChangeTable(
            data=related_changes[:50],
            orderable=False
        )

        objectchanges = ObjectChange.objects.restrict(request.user, 'view').filter(
            changed_object_type=objectchange.changed_object_type,
            changed_object_id=objectchange.changed_object_id,
        )

        next_change = objectchanges.filter(time__gt=objectchange.time).order_by('time').first()
        prev_change = objectchanges.filter(time__lt=objectchange.time).order_by('-time').first()

        if prev_change:
            diff_added = shallow_compare_dict(
                prev_change.object_data,
                objectchange.object_data,
                exclude=['last_updated'],
            )
            diff_removed = {x: prev_change.object_data.get(x) for x in diff_added}
        else:
            # No previous change; this is the initial change that added the object
            diff_added = diff_removed = objectchange.object_data

        return render(request, 'extras/objectchange.html', {
            'objectchange': objectchange,
            'diff_added': diff_added,
            'diff_removed': diff_removed,
            'next_change': next_change,
            'prev_change': prev_change,
            'related_changes_table': related_changes_table,
            'related_changes_count': related_changes.count()
        })


class ObjectChangeLogView(View):
    """
    Present a history of changes made to a particular object.
    """

    def get(self, request, model, **kwargs):

        # Handle QuerySet restriction of parent object if needed
        if hasattr(model.objects, 'restrict'):
            obj = get_object_or_404(model.objects.restrict(request.user, 'view'), **kwargs)
        else:
            obj = get_object_or_404(model, **kwargs)

        # Gather all changes for this object (and its related objects)
        content_type = ContentType.objects.get_for_model(model)
        objectchanges = ObjectChange.objects.restrict(request.user, 'view').prefetch_related(
            'user', 'changed_object_type'
        ).filter(
            Q(changed_object_type=content_type, changed_object_id=obj.pk) |
            Q(related_object_type=content_type, related_object_id=obj.pk)
        )
        objectchanges_table = tables.ObjectChangeTable(
            data=objectchanges,
            orderable=False
        )

        # Apply the request context
        paginate = {
            'paginator_class': EnhancedPaginator,
            'per_page': request.GET.get('per_page', settings.PAGINATE_COUNT)
        }
        RequestConfig(request, paginate).configure(objectchanges_table)

        # Check whether a header template exists for this model
        base_template = '{}/{}.html'.format(model._meta.app_label, model._meta.model_name)
        try:
            template.loader.get_template(base_template)
            object_var = model._meta.model_name
        except template.TemplateDoesNotExist:
            base_template = 'base.html'
            object_var = 'obj'

        return render(request, 'extras/object_changelog.html', {
            object_var: obj,
            'instance': obj,  # We'll eventually standardize on 'instance` for the object variable name
            'table': objectchanges_table,
            'base_template': base_template,
            'active_tab': 'changelog',
        })


#
# Image attachments
#

class ImageAttachmentEditView(ObjectEditView):
    queryset = ImageAttachment.objects.all()
    model_form = forms.ImageAttachmentForm

    def alter_obj(self, imageattachment, request, args, kwargs):
        if not imageattachment.pk:
            # Assign the parent object based on URL kwargs
            model = kwargs.get('model')
            imageattachment.parent = get_object_or_404(model, pk=kwargs['object_id'])
        return imageattachment

    def get_return_url(self, request, imageattachment):
        return imageattachment.parent.get_absolute_url()


class ImageAttachmentDeleteView(ObjectDeleteView):
    queryset = ImageAttachment.objects.all()

    def get_return_url(self, request, imageattachment):
        return imageattachment.parent.get_absolute_url()


#
# Reports
#

class ReportListView(ContentTypePermissionRequiredMixin, View):
    """
    Retrieve all of the available reports from disk and the recorded JobResult (if any) for each.
    """
    def get_required_permission(self):
        return 'extras.view_reportresult'

    def get(self, request):

        reports = get_reports()
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        results = {
            r.name: r
            for r in JobResult.objects.filter(
                obj_type=report_content_type,
                status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
            ).defer('data')
        }

        ret = []
        for module, report_list in reports:
            module_reports = []
            for report in report_list:
                report.result = results.get(report.full_name, None)
                module_reports.append(report)
            ret.append((module, module_reports))

        return render(request, 'extras/report_list.html', {
            'reports': ret,
        })


class ReportView(ContentTypePermissionRequiredMixin, View):
    """
    Display a single Report and its associated JobResult (if any).
    """
    def get_required_permission(self):
        return 'extras.view_reportresult'

    def get(self, request, module, name):

        report = get_report(module, name)
        if report is None:
            raise Http404

        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        report.result = JobResult.objects.filter(
            obj_type=report_content_type,
            name=report.full_name,
            status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
        ).first()

        return render(request, 'extras/report.html', {
            'report': report,
            'run_form': ConfirmationForm(),
        })

    def post(self, request, module, name):

        # Permissions check
        if not request.user.has_perm('extras.run_report'):
            return HttpResponseForbidden()

        report = get_report(module, name)
        if report is None:
            raise Http404

        # Allow execution only if RQ worker process is running
        if not Worker.count(get_connection('default')):
            messages.error(request, "Unable to run report: RQ worker process not running.")
            return render(request, 'extras/report.html', {
                'report': report,
            })

        # Run the Report. A new JobResult is created.
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        job_result = JobResult.enqueue_job(
            run_report,
            report.full_name,
            report_content_type,
            request.user
        )

        return redirect('extras:report_result', job_result_pk=job_result.pk)


class ReportResultView(ContentTypePermissionRequiredMixin, View):
    """
    Display a JobResult pertaining to the execution of a Report.
    """
    def get_required_permission(self):
        return 'extras.view_report'

    def get(self, request, job_result_pk):
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        jobresult = get_object_or_404(JobResult.objects.all(), pk=job_result_pk, obj_type=report_content_type)

        # Retrieve the Report and attach the JobResult to it
        module, report_name = jobresult.name.split('.')
        report = get_report(module, report_name)
        report.result = jobresult

        return render(request, 'extras/report_result.html', {
            'report': report,
            'result': jobresult,
        })


#
# Scripts
#

class GetScriptMixin:
    def _get_script(self, name, module=None):
        if module is None:
            module, name = name.split('.', 1)
        scripts = get_scripts()
        try:
            return scripts[module][name]()
        except KeyError:
            raise Http404


class ScriptListView(ContentTypePermissionRequiredMixin, View):

    def get_required_permission(self):
        return 'extras.view_script'

    def get(self, request):

        scripts = get_scripts(use_names=True)
        script_content_type = ContentType.objects.get(app_label='extras', model='script')
        results = {
            r.name: r
            for r in JobResult.objects.filter(
                obj_type=script_content_type,
                status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
            ).defer('data')
        }

        for _scripts in scripts.values():
            for script in _scripts.values():
                script.result = results.get(script.full_name)

        return render(request, 'extras/script_list.html', {
            'scripts': scripts,
        })


class ScriptView(ContentTypePermissionRequiredMixin, GetScriptMixin, View):

    def get_required_permission(self):
        return 'extras.view_script'

    def get(self, request, module, name):
        script = self._get_script(name, module)
        form = script.as_form(initial=request.GET)

        # Look for a pending JobResult (use the latest one by creation timestamp)
        script_content_type = ContentType.objects.get(app_label='extras', model='script')
        script.result = JobResult.objects.filter(
            obj_type=script_content_type,
            name=script.full_name,
        ).exclude(
            status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
        ).first()

        return render(request, 'extras/script.html', {
            'module': module,
            'script': script,
            'form': form,
        })

    def post(self, request, module, name):

        # Permissions check
        if not request.user.has_perm('extras.run_script'):
            return HttpResponseForbidden()

        script = self._get_script(name, module)
        form = script.as_form(request.POST, request.FILES)

        # Allow execution only if RQ worker process is running
        if not Worker.count(get_connection('default')):
            messages.error(request, "Unable to run script: RQ worker process not running.")

        elif form.is_valid():
            commit = form.cleaned_data.pop('_commit')

            script_content_type = ContentType.objects.get(app_label='extras', model='script')
            job_result = JobResult.enqueue_job(
                run_script,
                script.full_name,
                script_content_type,
                request.user,
                data=form.cleaned_data,
                request=copy_safe_request(request),
                commit=commit
            )

            return redirect('extras:script_result', job_result_pk=job_result.pk)

        return render(request, 'extras/script.html', {
            'module': module,
            'script': script,
            'form': form,
        })


class ScriptResultView(ContentTypePermissionRequiredMixin, GetScriptMixin, View):

    def get_required_permission(self):
        return 'extras.view_script'

    def get(self, request, job_result_pk):
        result = get_object_or_404(JobResult.objects.all(), pk=job_result_pk)
        script_content_type = ContentType.objects.get(app_label='extras', model='script')
        if result.obj_type != script_content_type:
            raise Http404

        script = self._get_script(result.name)

        return render(request, 'extras/script_result.html', {
            'script': script,
            'result': result,
            'class_name': script.__class__.__name__
        })
