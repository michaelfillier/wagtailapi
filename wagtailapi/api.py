from __future__ import absolute_import

import json
import urllib
from functools import wraps
from collections import OrderedDict

from django_filters.filterset import filterset_factory

from django.db import models
from django.utils.encoding import force_text
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, Http404
from django.shortcuts import get_object_or_404
from django.core.paginator import Paginator, EmptyPage
from django.core.serializers.json import DjangoJSONEncoder
from django.conf.urls import url

from wagtail.wagtailcore.models import Page
from wagtail.wagtailimages.models import get_image_model
from wagtail.wagtaildocs.models import Document
from wagtail.wagtailcore.utils import resolve_model_string
from wagtail.wagtailsearch.backends import get_search_backend


def get_api_data(obj, fields):
    # Find any child relations (pages only)
    child_relations = {}
    if isinstance(obj, Page):
        child_relations = {
            child_relation.field.rel.related_name: child_relation.model
            for child_relation in obj._meta.child_relations
        }

    # Loop through fields
    for field_name in fields:
        # Check child relations
        if field_name in child_relations and hasattr(child_relations[field_name], 'api_fields'):
            yield field_name, [
                dict(get_api_data(child_object, child_relations[field_name].api_fields))
                for child_object in getattr(obj, field_name).all()
            ]
            continue

        # Check django fields
        try:
            field = obj._meta.get_field_by_name(field_name)[0]
            yield field_name, field._get_val_from_obj(obj)
            continue
        except models.fields.FieldDoesNotExist:
            pass

        # Check attributes
        if hasattr(obj, field_name):
            value = getattr(obj, field_name)
            yield field_name, force_text(value, strings_only=True)
            continue


class BaseAPIEndpoint(object):
    class BadRequestError(Exception):
        pass

    def listing_view(self, request):
        pass

    def detail_view(self, request, pk):
        pass

    def get_api_fields(self, model):
        api_fields = []

        if hasattr(model, 'api_fields'):
            api_fields.extend(model.api_fields)

        return api_fields

    def serialize_object_metadata(self, obj, show_details=False):
        return OrderedDict()

    def serialize_object(self, obj, fields=(), all_fields=False, show_details=False):
        data = [
            ('id', obj.id),
        ]

        # Add meta
        metadata = self.serialize_object_metadata(obj, show_details=show_details)
        if metadata:
            data.append(('meta', metadata))

        # Add other fields
        api_fields = self.get_api_fields(type(obj))
        if all_fields:
            fields = api_fields
        else:
            fields = [field for field in fields if field in api_fields]

        data.extend(get_api_data(obj, fields))

        return OrderedDict(data)

    def do_field_filtering(self, request, queryset):
        # Get filterset class
        filterset_class = filterset_factory(queryset.model)

        # Run field filters
        return filterset_class(request.GET, queryset=queryset).qs

    def do_ordering(self, request, queryset):
        if 'order' in request.GET:
            order_by = request.GET['order']

            # Check if reverse ordering is set
            if order_by.startswith('-'):
                reverse_order = True
                order_by = order_by[1:]
            else:
                reverse_order = False

            # Add ordering
            if order_by == 'id' or order_by in self.get_api_fields(queryset.model):
                queryset = queryset.order_by(order_by)
            else:
                # Unknown field
                raise self.BadRequestError("cannot order by '%s' (unknown field)" % order_by)

            # Reverse order
            if reverse_order:
                queryset = queryset.reverse()

        return queryset

    def do_search(self, request, queryset):
        if 'search' in request.GET:
            search_query = request.GET['search']

            sb = get_search_backend()
            queryset = sb.search(search_query, queryset)

        return queryset

    def do_pagination(self, request, queryset):
        try:
            offset = int(request.GET.get('offset', 0))
            assert offset >= 0
        except (ValueError, AssertionError):
            raise self.BadRequestError("offset must be a positive integer")

        try:
            limit = int(request.GET.get('limit', 20))
            assert limit >= 0
        except (ValueError, AssertionError):
            raise self.BadRequestError("limit must be a positive integer")

        start = offset
        stop = offset + limit

        return queryset[start:stop]

    def json_response(self, data, response_cls=HttpResponse):
        return response_cls(
            json.dumps(data, indent=4, cls=DjangoJSONEncoder),
            content_type='application/json'
        )

    def api_view(self, view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            # Catch exceptions and format them as JSON documents
            try:
                return view(*args, **kwargs)
            except Http404 as e:
                return self.json_response({
                    'message': str(e)
                }, response_cls=HttpResponseNotFound)
            except self.BadRequestError as e:
                return self.json_response({
                    'message': str(e)
                }, response_cls=HttpResponseBadRequest)

        return wrapper

    def get_urlpatterns(self):
        return [
            url(r'^$', self.api_view(self.listing_view), name='listing'),
            url(r'^(\d+)/$', self.api_view(self.detail_view), name='detail'),
        ]


class PagesAPIEndpoint(BaseAPIEndpoint):
    def get_queryset(self, request, model=Page):
        # Get live pages that are not in a private section
        queryset = model.objects.public().live()

        # Filter by site
        queryset = queryset.descendant_of(request.site.root_page, inclusive=True)

        return queryset

    def get_api_fields(self, model):
        api_fields = ['title']
        api_fields.extend(super(PagesAPIEndpoint, self).get_api_fields(model))
        return api_fields

    def serialize_object_metadata(self, page, show_details=False):
        data = super(PagesAPIEndpoint, self).serialize_object_metadata(page, show_details=show_details)

        # Add type
        data['type'] = page.specific_class._meta.app_label + '.' + page.specific_class.__name__

        # Add parent id
        if show_details:
            data['parent_id'] = page.get_parent().id

        return data

    def get_model(self, request):
        if 'type' not in request.GET:
            return Page

        model_name = request.GET['type']
        try:
            return resolve_model_string(model_name)
        except LookupError:
            raise Http404("Type doesn't exist")

    def do_child_of_filter(self, request, queryset):
        if 'child_of' in request.GET:
            parent_page_id = request.GET['child_of']

            try:
                parent_page = Page.objects.get(id=parent_page_id)
                return queryset.child_of(parent_page)
            except Page.DoesNotExist:
                raise Http404("Parent page doesn't exist")

        return queryset

    def listing_view(self, request):
        # Get model and queryset
        model = self.get_model(request)
        queryset = self.get_queryset(request, model=model)

        # Filtering
        queryset = self.do_field_filtering(request, queryset)
        queryset = self.do_child_of_filter(request, queryset)

        # Ordering
        queryset = self.do_ordering(request, queryset)

        # Search
        queryset = self.do_search(request, queryset)

        # Pagination
        total_count = queryset.count()
        queryset = self.do_pagination(request, queryset)

        # Get list of fields to show in results
        if 'fields' in request.GET:
            fields = request.GET['fields'].split(',')
        else:
            fields = ('title', )

        return self.json_response(
            OrderedDict([
                ('meta', OrderedDict([
                    ('total_count', total_count),
                ])),
                ('pages', [
                    self.serialize_object(page, fields=fields)
                    for page in queryset
                ]),
            ])
        )

    def detail_view(self, request, pk):
        page = get_object_or_404(self.get_queryset(request), pk=pk).specific
        data = self.serialize_object(page, all_fields=True, show_details=True)

        return self.json_response(data)


class ImagesAPIEndpoint(BaseAPIEndpoint):
    model = get_image_model()

    def get_queryset(self, request):
        return self.model.objects.all()

    def get_api_fields(self, model):
        api_fields = ['title', 'width', 'height']
        api_fields.extend(super(ImagesAPIEndpoint, self).get_api_fields(model))
        return api_fields

    def listing_view(self, request):
        queryset = self.get_queryset(request)

        # Filtering
        queryset = self.do_field_filtering(request, queryset)

        # Ordering
        queryset = self.do_ordering(request, queryset)

        # Search
        queryset = self.do_search(request, queryset)

        # Pagination
        total_count = queryset.count()
        queryset = self.do_pagination(request, queryset)

        # Get list of fields to show in results
        if 'fields' in request.GET:
            fields = request.GET['fields'].split(',')
        else:
            fields = ('title', )

        return self.json_response(
            OrderedDict([
                ('meta', OrderedDict([
                    ('total_count', total_count),
                ])),
                ('images', [
                    self.serialize_object(image, fields=fields)
                    for image in queryset
                ]),
            ])
        )

    def detail_view(self, request, pk):
        image = get_object_or_404(self.get_queryset(request), pk=pk)
        data = self.serialize_object(image, all_fields=True)

        return self.json_response(data)


class DocumentsAPIEndpoint(BaseAPIEndpoint):
    def get_api_fields(self, model):
        api_fields = ['title']
        api_fields.extend(super(DocumentsAPIEndpoint, self).get_api_fields(model))
        return api_fields

    def listing_view(self, request):
        queryset = Document.objects.all()

        # Filtering
        queryset = self.do_field_filtering(request, queryset)

        # Ordering
        queryset = self.do_ordering(request, queryset)

        # Search
        queryset = self.do_search(request, queryset)

        # Pagination
        total_count = queryset.count()
        queryset = self.do_pagination(request, queryset)

        return self.json_response(
            OrderedDict([
                ('meta', OrderedDict([
                    ('total_count', total_count),
                ])),
                ('documents', [
                    self.serialize_object(document, fields=('title', ))
                    for document in queryset
                ]),
            ])
        )

    def detail_view(self, request, pk):
        document = get_object_or_404(Document, pk=pk)
        data = self.serialize_object(document, all_fields=True)

        return self.json_response(data)
