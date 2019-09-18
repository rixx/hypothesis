# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2019 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import absolute_import, division, print_function

import unittest
from collections import defaultdict
from functools import partial

import django.db.models as dm
import django.forms as df
import django.test as dt
from django.core.exceptions import ValidationError
from django.db import IntegrityError

import hypothesis._strategies as st
from hypothesis import reject
from hypothesis.errors import InvalidArgument
from hypothesis.extra.django._fields import from_field
from hypothesis.utils.conventions import infer

if False:
    from datetime import tzinfo  # noqa
    from typing import Any, Type, Optional, List, Text, Callable, Union  # noqa
    from hypothesis.utils.conventions import InferType  # noqa


class HypothesisTestCase(object):
    def setup_example(self):
        self._pre_setup()

    def teardown_example(self, example):
        self._post_teardown()

    def __call__(self, result=None):
        testMethod = getattr(self, self._testMethodName)
        if getattr(testMethod, u"is_hypothesis_test", False):
            return unittest.TestCase.__call__(self, result)
        else:
            return dt.SimpleTestCase.__call__(self, result)


class TestCase(HypothesisTestCase, dt.TestCase):
    pass


class TransactionTestCase(HypothesisTestCase, dt.TransactionTestCase):
    pass


@st.defines_strategy
def from_model(
    model,  # type: Type[dm.Model]
    __infer_related_fields=False,
    **field_strategies  # type: Union[st.SearchStrategy[Any], InferType]
):
    # type: (...) -> st.SearchStrategy[Any]
    """Return a strategy for examples of ``model``.

    .. warning::
        Hypothesis creates saved models. This will run inside your testing
        transaction when using the test runner, but if you use the dev console
        this will leave debris in your database.

    ``model`` must be an subclass of :class:`~django:django.db.models.Model`.
    Strategies for fields may be passed as keyword arguments, for example
    ``is_staff=st.just(False)``.

    Hypothesis can often infer a strategy based the field type and validators,
    and will attempt to do so for any required fields.  No strategy will be
    inferred for an :class:`~django:django.db.models.AutoField`, nullable field,
    foreign key, or field for which a keyword
    argument is passed to ``from_model()``.  For example,
    a Shop type with a foreign key to Company could be generated with::

        shop_strategy = from_model(Shop, company=from_model(Company))

    Like for :func:`~hypothesis.strategies.builds`, you can pass
    :obj:`~hypothesis.infer` as a keyword argument to infer a strategy for
    a field which has a default value instead of using the default.
    """
    if not issubclass(model, dm.Model):
        raise InvalidArgument("model=%r must be a subtype of Model" % (model,))

    fields_by_name = {f.name: f for f in model._meta.concrete_fields}
    field_context = defaultdict(dict)
    for name, value in sorted(field_strategies.items()):
        if '__' in name:
            field_name, _, field_value = name.partition('__')
            field_context[field_name][field_value] = value
            field_strategies.pop(name)
        elif value is infer:
            field_strategies[name] = from_field(fields_by_name[name])
    for name, field in sorted(fields_by_name.items()):
        if (
            name not in field_strategies
            and not field.auto_created
            and field.default is dm.fields.NOT_PROVIDED
        ):
            field_strategies[name] = from_field(
                field,
                __context=field_context.get(name),
                __infer_related_fields=__infer_related_fields,
            )

    for field in field_strategies:
        if model._meta.get_field(field).primary_key:
            # The primary key is generated as part of the strategy. We
            # want to find any existing row with this primary key and
            # overwrite its contents.
            kwargs = {field: field_strategies.pop(field)}
            kwargs["defaults"] = st.fixed_dictionaries(field_strategies)  # type: ignore
            return _models_impl(st.builds(model.objects.update_or_create, **kwargs))

    # The primary key is not generated as part of the strategy, so we
    # just match against any row that has the same value for all
    # fields.
    return _models_impl(st.builds(model.objects.get_or_create, **field_strategies))


@st.composite
def _models_impl(draw, strat):
    """Handle the nasty part of drawing a value for models()"""
    try:
        return draw(strat)[0]
    except IntegrityError:
        reject()


@st.defines_strategy
def from_form(
    form,  # type: Type[dm.Model]
    form_kwargs=None,  # type: dict
    **field_strategies  # type: Union[st.SearchStrategy[Any], InferType]
):
    # type: (...) -> st.SearchStrategy[Any]
    """Return a strategy for examples of ``form``.

    ``form`` must be an subclass of :class:`~django:django.forms.Form`.
    Strategies for fields may be passed as keyword arguments, for example
    ``is_staff=st.just(False)``.

    Hypothesis can often infer a strategy based the field type and validators,
    and will attempt to do so for any required fields.  No strategy will be
    inferred for a disabled field or field for which a keyword argument
    is passed to ``from_form()``.

    This function uses the fields of an unbound ``form`` instance to determine
    field strategies, any keyword arguments needed to instantiate the unbound
    ``form`` instance can be passed into ``from_form()`` as a dict with the
    keyword ``form_kwargs``. E.g.::

        shop_strategy = from_form(Shop, form_kwargs={"company_id": 5})

    Like for :func:`~hypothesis.strategies.builds`, you can pass
    :obj:`~hypothesis.infer` as a keyword argument to infer a strategy for
    a field which has a default value instead of using the default.
    """
    # currently unsupported:
    # ComboField
    # FilePathField
    # FileField
    # ImageField
    form_kwargs = form_kwargs or {}
    if not issubclass(form, df.BaseForm):
        raise InvalidArgument("form=%r must be a subtype of Form" % (form,))

    # Forms are a little bit different from models. Model classes have
    # all their fields defined, whereas forms may have different fields
    # per-instance. So, we ought to instantiate the form and get the
    # fields from the instance, thus we need to accept the kwargs for
    # instantiation as well as the explicitly defined strategies

    unbound_form = form(**form_kwargs)
    fields_by_name = {}
    for name, field in unbound_form.fields.items():
        if isinstance(field, df.MultiValueField):
            # PS: So this is a little strange, but MultiValueFields must
            # have their form data encoded in a particular way for the
            # values to actually be picked up by the widget instances'
            # ``value_from_datadict``.
            # E.g. if a MultiValueField named 'mv_field' has 3
            # sub-fields then the ``value_from_datadict`` will look for
            # 'mv_field_0', 'mv_field_1', and 'mv_field_2'. Here I'm
            # decomposing the individual sub-fields into the names that
            # the form validation process expects
            for i, _field in enumerate(field.fields):
                fields_by_name["%s_%d" % (name, i)] = _field
        else:
            fields_by_name[name] = field
    for name, value in sorted(field_strategies.items()):
        if value is infer:
            field_strategies[name] = from_field(fields_by_name[name])

    for name, field in sorted(fields_by_name.items()):
        if name not in field_strategies and not field.disabled:
            field_strategies[name] = from_field(field)

    return _forms_impl(
        st.builds(
            partial(form, **form_kwargs),
            data=st.fixed_dictionaries(field_strategies),  # type: ignore
        )
    )


@st.composite
def _forms_impl(draw, strat):
    """Handle the nasty part of drawing a value for from_form()"""
    try:
        return draw(strat)
    except ValidationError:
        reject()
