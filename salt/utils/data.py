# -*- coding: utf-8 -*-

from __future__ import absolute_import

# Import Python libs
import collections
import copy
import fnmatch
import logging
import re
import yaml

# Import Salt libs
import salt.utils.dictupdate
from salt.defaults import DEFAULT_TARGET_DELIM
from salt.exceptions import SaltException
from salt.utils.decorators.jinja import jinja_filter

# Import 3rd-party libs
from salt.ext import six
from salt.ext.six.moves import range  # pylint: disable=redefined-builtin

log = logging.getLogger(__name__)


@jinja_filter('compare_dicts')
def compare_dicts(old=None, new=None):
    '''
    Compare before and after results from various salt functions, returning a
    dict describing the changes that were made.
    '''
    ret = {}
    for key in set((new or {})).union((old or {})):
        if key not in old:
            # New key
            ret[key] = {'old': '',
                        'new': new[key]}
        elif key not in new:
            # Key removed
            ret[key] = {'new': '',
                        'old': old[key]}
        elif new[key] != old[key]:
            # Key modified
            ret[key] = {'old': old[key],
                        'new': new[key]}
    return ret


@jinja_filter('compare_lists')
def compare_lists(old=None, new=None):
    '''
    Compare before and after results from various salt functions, returning a
    dict describing the changes that were made
    '''
    ret = dict()
    for item in new:
        if item not in old:
            ret['new'] = item
    for item in old:
        if item not in new:
            ret['old'] = item
    return ret


@jinja_filter('json_decode_dict')
def decode_dict(data):
    '''
    JSON decodes as unicode, Jinja needs bytes...
    '''
    rv = {}
    for key, value in six.iteritems(data):
        if isinstance(key, six.text_type) and six.PY2:
            key = key.encode('utf-8')
        if isinstance(value, six.text_type) and six.PY2:
            value = value.encode('utf-8')
        elif isinstance(value, list):
            value = decode_list(value)
        elif isinstance(value, dict):
            value = decode_dict(value)
        rv[key] = value
    return rv


@jinja_filter('json_decode_list')
def decode_list(data):
    '''
    JSON decodes as unicode, Jinja needs bytes...
    '''
    rv = []
    for item in data:
        if isinstance(item, six.text_type) and six.PY2:
            item = item.encode('utf-8')
        elif isinstance(item, list):
            item = decode_list(item)
        elif isinstance(item, dict):
            item = decode_dict(item)
        rv.append(item)
    return rv


@jinja_filter('exactly_n_true')
def exactly_n(l, n=1):
    '''
    Tests that exactly N items in an iterable are "truthy" (neither None,
    False, nor 0).
    '''
    i = iter(l)
    return all(any(i) for j in range(n)) and not any(i)


@jinja_filter('exactly_one_true')
def exactly_one(l):
    '''
    Check if only one item is not None, False, or 0 in an iterable.
    '''
    return exactly_n(l)


def traverse_dict(data, key, default=None, delimiter=DEFAULT_TARGET_DELIM):
    '''
    Traverse a dict using a colon-delimited (or otherwise delimited, using the
    'delimiter' param) target string. The target 'foo:bar:baz' will return
    data['foo']['bar']['baz'] if this value exists, and will otherwise return
    the dict in the default argument.
    '''
    try:
        for each in key.split(delimiter):
            data = data[each]
    except (KeyError, IndexError, TypeError):
        # Encountered a non-indexable value in the middle of traversing
        return default
    return data


def filter_by(lookup_dict,
              lookup,
              traverse,
              merge=None,
              default='default',
              base=None):
    '''
    Common code to filter data structures like grains and pillar
    '''
    ret = None
    # Default value would be an empty list if lookup not found
    val = traverse_dict_and_list(traverse, lookup, [])

    # Iterate over the list of values to match against patterns in the
    # lookup_dict keys
    for each in val if isinstance(val, list) else [val]:
        for key in lookup_dict:
            test_key = key if isinstance(key, six.string_types) else str(key)
            test_each = each if isinstance(each, six.string_types) else str(each)
            if fnmatch.fnmatchcase(test_each, test_key):
                ret = lookup_dict[key]
                break
        if ret is not None:
            break

    if ret is None:
        ret = lookup_dict.get(default, None)

    if base and base in lookup_dict:
        base_values = lookup_dict[base]
        if ret is None:
            ret = base_values

        elif isinstance(base_values, collections.Mapping):
            if not isinstance(ret, collections.Mapping):
                raise SaltException(
                    'filter_by default and look-up values must both be '
                    'dictionaries.')
            ret = salt.utils.dictupdate.update(copy.deepcopy(base_values), ret)

    if merge:
        if not isinstance(merge, collections.Mapping):
            raise SaltException(
                'filter_by merge argument must be a dictionary.')

        if ret is None:
            ret = merge
        else:
            salt.utils.dictupdate.update(ret, copy.deepcopy(merge))

    return ret


def traverse_dict_and_list(data, key, default=None, delimiter=DEFAULT_TARGET_DELIM):
    '''
    Traverse a dict or list using a colon-delimited (or otherwise delimited,
    using the 'delimiter' param) target string. The target 'foo:bar:0' will
    return data['foo']['bar'][0] if this value exists, and will otherwise
    return the dict in the default argument.
    Function will automatically determine the target type.
    The target 'foo:bar:0' will return data['foo']['bar'][0] if data like
    {'foo':{'bar':['baz']}} , if data like {'foo':{'bar':{'0':'baz'}}}
    then return data['foo']['bar']['0']
    '''
    for each in key.split(delimiter):
        if isinstance(data, list):
            try:
                idx = int(each)
            except ValueError:
                embed_match = False
                # Index was not numeric, lets look at any embedded dicts
                for embedded in (x for x in data if isinstance(x, dict)):
                    try:
                        data = embedded[each]
                        embed_match = True
                        break
                    except KeyError:
                        pass
                if not embed_match:
                    # No embedded dicts matched, return the default
                    return default
            else:
                try:
                    data = data[idx]
                except IndexError:
                    return default
        else:
            try:
                data = data[each]
            except (KeyError, TypeError):
                return default
    return data


def subdict_match(data,
                  expr,
                  delimiter=DEFAULT_TARGET_DELIM,
                  regex_match=False,
                  exact_match=False):
    '''
    Check for a match in a dictionary using a delimiter character to denote
    levels of subdicts, and also allowing the delimiter character to be
    matched. Thus, 'foo:bar:baz' will match data['foo'] == 'bar:baz' and
    data['foo']['bar'] == 'baz'. The former would take priority over the
    latter.
    '''
    def _match(target, pattern, regex_match=False, exact_match=False):
        if regex_match:
            try:
                return re.match(pattern.lower(), str(target).lower())
            except Exception:
                log.error('Invalid regex \'{0}\' in match'.format(pattern))
                return False
        elif exact_match:
            return str(target).lower() == pattern.lower()
        else:
            return fnmatch.fnmatch(str(target).lower(), pattern.lower())

    def _dict_match(target, pattern, regex_match=False, exact_match=False):
        wildcard = pattern.startswith('*:')
        if wildcard:
            pattern = pattern[2:]

        if pattern == '*':
            # We are just checking that the key exists
            return True
        elif pattern in target:
            # We might want to search for a key
            return True
        elif subdict_match(target,
                           pattern,
                           regex_match=regex_match,
                           exact_match=exact_match):
            return True
        if wildcard:
            for key in target:
                if _match(key,
                          pattern,
                          regex_match=regex_match,
                          exact_match=exact_match):
                    return True
                if isinstance(target[key], dict):
                    if _dict_match(target[key],
                                   pattern,
                                   regex_match=regex_match,
                                   exact_match=exact_match):
                        return True
                elif isinstance(target[key], list):
                    for item in target[key]:
                        if _match(item,
                                  pattern,
                                  regex_match=regex_match,
                                  exact_match=exact_match):
                            return True
        return False

    for idx in range(1, expr.count(delimiter) + 1):
        splits = expr.split(delimiter)
        key = delimiter.join(splits[:idx])
        matchstr = delimiter.join(splits[idx:])
        log.debug('Attempting to match \'{0}\' in \'{1}\' using delimiter '
                  '\'{2}\''.format(matchstr, key, delimiter))
        match = traverse_dict_and_list(data, key, {}, delimiter=delimiter)
        if match == {}:
            continue
        if isinstance(match, dict):
            if _dict_match(match,
                           matchstr,
                           regex_match=regex_match,
                           exact_match=exact_match):
                return True
            continue
        if isinstance(match, list):
            # We are matching a single component to a single list member
            for member in match:
                if isinstance(member, dict):
                    if _dict_match(member,
                                   matchstr,
                                   regex_match=regex_match,
                                   exact_match=exact_match):
                        return True
                if _match(member,
                          matchstr,
                          regex_match=regex_match,
                          exact_match=exact_match):
                    return True
            continue
        if _match(match,
                  matchstr,
                  regex_match=regex_match,
                  exact_match=exact_match):
            return True
    return False


@jinja_filter('substring_in_list')
def substr_in_list(string_to_search_for, list_to_search):
    '''
    Return a boolean value that indicates whether or not a given
    string is present in any of the strings which comprise a list
    '''
    return any(string_to_search_for in s for s in list_to_search)


def is_dictlist(data):
    '''
    Returns True if data is a list of one-element dicts (as found in many SLS
    schemas), otherwise returns False
    '''
    if isinstance(data, list):
        for element in data:
            if isinstance(element, dict):
                if len(element) != 1:
                    return False
            else:
                return False
        return True
    return False


def repack_dictlist(data,
                    strict=False,
                    recurse=False,
                    key_cb=None,
                    val_cb=None):
    '''
    Takes a list of one-element dicts (as found in many SLS schemas) and
    repacks into a single dictionary.
    '''
    if isinstance(data, six.string_types):
        try:
            data = yaml.safe_load(data)
        except yaml.parser.ParserError as err:
            log.error(err)
            return {}

    if key_cb is None:
        key_cb = lambda x: x
    if val_cb is None:
        val_cb = lambda x, y: y

    valid_non_dict = (six.string_types, six.integer_types, float)
    if isinstance(data, list):
        for element in data:
            if isinstance(element, valid_non_dict):
                continue
            elif isinstance(element, dict):
                if len(element) != 1:
                    log.error(
                        'Invalid input for repack_dictlist: key/value pairs '
                        'must contain only one element (data passed: %s).',
                        element
                    )
                    return {}
            else:
                log.error(
                    'Invalid input for repack_dictlist: element %s is '
                    'not a string/dict/numeric value', element
                )
                return {}
    else:
        log.error(
            'Invalid input for repack_dictlist, data passed is not a list '
            '(%s)', data
        )
        return {}

    ret = {}
    for element in data:
        if isinstance(element, valid_non_dict):
            ret[key_cb(element)] = None
        else:
            key = next(iter(element))
            val = element[key]
            if is_dictlist(val):
                if recurse:
                    ret[key_cb(key)] = repack_dictlist(val, recurse=recurse)
                elif strict:
                    log.error(
                        'Invalid input for repack_dictlist: nested dictlist '
                        'found, but recurse is set to False'
                    )
                    return {}
                else:
                    ret[key_cb(key)] = val_cb(key, val)
            else:
                ret[key_cb(key)] = val_cb(key, val)
    return ret
