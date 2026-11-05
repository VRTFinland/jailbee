import re

from jailbee.services import log_query


def test_select_lines_union_of_patterns():
    assert log_query.select_lines(['a', 'b', 'c'], [re.compile('a'), re.compile('c')], limit=3, before=0, after=0) == ['a', '--', 'c']


def test_select_lines_limit_keeps_last_matches():
    assert log_query.select_lines(['x1', 'x2', 'x3'], [re.compile('x')], limit=2, before=0, after=0) == ['x2', 'x3']


def test_context_merges_overlaps_and_separates_groups():
    lines = ['0', 'hit', '2', 'hit', '4', '5', '6', 'hit', '8']
    assert log_query.select_lines(lines, [re.compile('hit')], limit=3, before=1, after=1) == lines[:5] + ['--'] + lines[6:]


def test_strip_terminal():
    assert log_query.strip_terminal('\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07ok\x1b]8;;url\x1b\\end') == 'red\nokend'
