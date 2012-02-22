# coding: utf8
"""
    tinycss.parser
    --------------

    Simple recursive-descent parser for the CSS core syntax:
    http://www.w3.org/TR/CSS21/syndata.html#tokenization

    :copyright: (c) 2010 by Simon Sapin.
    :license: BSD, see LICENSE for more details.
"""

from __future__ import unicode_literals, print_function
from itertools import chain
import functools
import sys
import re

from .tokenizer import tokenize, COMPILED_MACROS


#  stylesheet  : [ CDO | CDC | S | statement ]*;
#  statement   : ruleset | at-rule;
#  at-rule     : ATKEYWORD S* any* [ block | ';' S* ];
#  block       : '{' S* [ any | block | ATKEYWORD S* | ';' S* ]* '}' S*;
#  ruleset     : selector? '{' S* declaration? [ ';' S* declaration? ]* '}' S*;
#  selector    : any+;
#  declaration : property S* ':' S* value;
#  property    : IDENT;
#  value       : [ any | block | ATKEYWORD S* ]+;
#  any         : [ IDENT | NUMBER | PERCENTAGE | DIMENSION | STRING
#                | DELIM | URI | HASH | UNICODE-RANGE | INCLUDES
#                | DASHMATCH | ':' | FUNCTION S* [any|unused]* ')'
#                | '(' S* [any|unused]* ')' | '[' S* [any|unused]* ']'
#                ] S*;
#  unused      : block | ATKEYWORD S* | ';' S* | CDO S* | CDC S*;


def parse(string):
    """Same a :func:`parse_stylesheet`, but takes CSS as an unicode string.
    """
    tokens = regroup(iter(tokenize(string)))
    return parse_stylesheet(tokens)


class ContainerToken(object):
    """A token that contains other (nested) tokens."""
    def __init__(self, type_, css_start, css_end, content, line, column):
        self.type = type_
        self.css_start = css_start
        self.css_end = css_end
        self.content = content
        self.line = line
        self.column = column

    @property
    def as_css(self):
        parts = [self.css_start]
        parts.extend(token.as_css for token in self.content)
        parts.append(self.css_end)
        return ''.join(parts)


    format_string = '<ContainerToken {0.type} at {0.line}:{0.column}>'

    def __repr__(self):
        return (format_string + ' {0.content}').format(self)

    def pretty(self):
        lines = [self.format_string.format(self)]
        for token in self.content:
            for line in token.pretty().splitlines():
                lines.append('    ' + line)
        return '\n'.join(lines)


class FunctionToken(ContainerToken):
    """Specialized :class:`ContainerToken` that also hold a function name."""
    def __init__(self, type_, css_start, css_end, function_name, content,
                 line, column):
        super(FunctionToken, self).__init__(
            type_, css_start, css_end, content, line, column)
        self.function_name = function_name

    format_string = '<FunctionToken {0.function_name}() at {0.line}:{0.column}>'



def regroup(tokens, stop_at=None):
    """
    Match pairs of tokens: () [] {} function()
    (Strings in "" or '' are taken care of by the tokenizer.)

    Opening tokens are replaced by a  :class:`ContainerToken`.
    Closing tokens are removed. Unmatched closing tokens are invalid
    but left as-is. All nested structures that are still open at
    the end of the stylesheet are implicitly closed.

    :param tokens:
        a *flat* iterator of tokens, as returned by
        :func:`~tinycss.tokenizer.tokenize`
    :param stop_at:
        only used for recursion
    :return:
        A tree of tokens.

    """
    pairs = {'FUNCTION': ')', '(': ')', '[': ']', '{': '}'}
    for token in tokens:
        type_ = token.type
        if type_ == stop_at:
            return

        end = pairs.get(type_)
        if end is None:
            yield token  # Not a grouping token
        else:
            content = list(regroup(tokens, end))
            if type_ == 'FUNCTION':
                yield FunctionToken(token.type, token.as_css, end,
                                    token.value, content,
                                    token.line, token.column)
            else:
                yield ContainerToken(token.type, token.as_css, end,
                                     content,
                                     token.line, token.column)


class ParseError(ValueError):
    """A recoverable parsing error."""
    def __init__(self, token, reason):
        self.token = token
        self.message = 'Parse error at {}:{}, {}'.format(
            token.line, token.column, reason)

    def __repr__(self):
        return '<{0}: {1}>'.format(type(self).__name__, self.message)


class UnexpectedTokenError(ParseError):
    """A special kind of parsing error: a token of the wrong type was found."""
    def __init__(self, token, context):
        super(UnexpectedToken, self).__init__(
            token, 'unexpected {} token in {}'.format(token.type, context))


def parse_stylesheet(tokens):
    """Parse an stylesheet.

    :param tokens:
        an iterable of tokens.
    :return:
        a tuple of a list of rules and an a list of :class:`ParseError`.

        * At-rules are tuples of at-keyword, head and body as returned by
          :func:`parse_at_rule`
        * Rulesets are tuples of at-keyword, selector and declaration list,
          where the at-keyword is always ``None``. This helps telling them
          apart from at-rules. (See :func:`parse_ruleset`.)

    :raises:
        :class:`ParseError` if the at-rule is invalid for the core grammar.
        Note a that an at-rule can be valid for the core grammar but
        not for CSS 2.1 or another level.

    """
    rules = []
    errors = []
    for token in tokens:
        if token.type not in ('S', 'CDO', 'CDC'):
            try:
                if token.type == 'ATKEYWORD':
                    rules.append(parse_at_rule(token, tokens))
                else:
                    selector, declarations, rule_errors = parse_ruleset(
                        token, tokens)
                    rules.append((None, selector, declarations))
                    errors.extend(rule_errors)
            except ParseError as e:
                errors.append(e)
                # Skip the entire rule
    return rules, errors


def parse_at_rule(at_keyword_token, tokens):
    """Parse an at-rule.

    :param at_keyword_token:
        The ATKEYWORD token that start this at-rule
        You may have read it already to distinguish the rule from a ruleset.
    :param tokens:
        an iterator of subsequent tokens. Will be consumed just enough
        for one at-rule.
    :return:
        a tuple of an at-keyword, a head and a body

        * The at-keyword is a lower-case string, eg. '@import'
        * The head is a (possibly empty) list of tokens
        * The body is a block token, or ``None`` if the at-rule ends with ';'.

    :raises:
        :class:`ParseError` if the head is invalid for the core grammar.
        The body is **not** validated. This is because it might contain
        declarations. In case of an error in a declaration parsing should
        continue from the next declaration; the whole rule should not
        be ignored.
        You are expected to parse and validate (or ignore) at-rules yourself.

    """
    # CSS syntax is case-insensitive
    at_keyword = at_keyword_token.value.lower()
    head = []
    for token in tokens:
        if token.type in '{;':
            for head_token in head:
                validate_any(head_token.value, 'at-rule head')
            if token.type == '{':
                body = token
            else:
                body = None
            return at_keyword, head, body
        # Ignore white space just after the at-keyword, but keep it afterwards
        elif head or token.type != 'S':
            head.append(token)


def parse_ruleset(first_token, tokens):
    """Parse a ruleset: a selector followed by declaration block.

    :param first_token:
        The first token of the ruleset (probably of the selector).
        You may have read it already to distinguish the rule from an at-rule.
    :param tokens:
        an iterator of subsequent tokens. Will be consumed just enough
        for one ruleset.
    :return:
        a tuple of a selector, a declarations list and an error list.

        * The selector is a (possibly empty) new :class:`ContainerToken`
        * The declaration list is as returned by :func:`parse_declaration_list`
        * The errors are recovered :class:`ParseError` in declarations.
          (Parsing continues from the next declaration on such errors.)

    :raises:
        :class:`ParseError` if the selector is invalid for the core grammar.
        Note a that a selector can be valid for the core grammar but
        not for CSS 2.1 or another level.

    """
    selector_parts = []
    for token in chain([first_token], tokens):
        if token.type == '{':
            # Parse/validate once we’ve read the whole rule
            for selector_token in selector_parts:
                validate_any(selector_token, 'selector')
            start = selector_parts[0] if selector_parts else token
            selector = ContainerToken(
                'SELECTOR', '', '', selector_parts, start.line, start.column)
            declarations, errors = parse_declaration_list(token.content)
            return selector, declarations, errors
        else:
            selector_parts.append(token)


def parse_declaration_list(tokens):
    """Parse a ';' separated declaration list.

    If you have a block that contains declarations but not only
    (like ``@page`` in CSS 3 Paged Media), you need to extract them
    yourself and use :func:`parse_declaration` directly.

    :param tokens:
        an iterable of tokens. Should stop at (before) the end of the block,
        as marked by a '}'.
    :return:
        a tuple of the list of valid declarations as returned by
        :func:`parse_declaration` and a list of :class:`ParseError`

    """
    # split at ';'
    parts = []
    this_part = []
    for token in tokens:
        type_ = token.type
        if type_ == ';' and this_part:
            parts.append(this_part)
            this_part = []
        # skip white space at the start
        elif this_part or type_ != 'S':
            this_part.append(token)
    if this_part:
        parts.append(this_part)

    declarations = []
    errors = []
    for part in parts:
        try:
            declarations.append(parse_declaration(part))
        except ParseError as e:
            errors.append(e)
            # Skip the entire declaration
    return declarations, errors


def parse_declaration(tokens):
    """Parse a single declaration.

    :param tokens:
        an iterable of at least one token. Should stop at (before)
        the end of the declaration, as marked by a ';' or '}'.
        Empty declarations (ie. consecutive ';' with only white space
        in-between) should skipped and not passed to this function.
    :returns:
        a tuple of the property name as a lower-case string and the
        value list as returned by :func:`parse_value`.
    :raises:
        :class:`ParseError` if the tokens do not match the 'declaration'
        production of the core grammar.

    """
    tokens = iter(tokens)

    token = next(tokens)  # assume there is at least one
    if token.type == 'IDENT':
        # CSS syntax is case-insensitive
        property_name = token.value.lower()
    else:
        raise UnexpectedToken(token, ', expected a property name')

    for token in tokens:
        if token.type == ':':
            break
        elif token.type != 'S':
            raise UnexpectedToken(token, ", expected ':'")
    else:
        raise ParseError(token, "expected ':'")

    value = parse_value(tokens)
    if not value:
        raise ParseError(token, 'expected a property value')
    return property_name, value


def parse_value(tokens):
    """Parse a property value and return a list of tokens.

    :param tokens:
        an iterable of tokens
    :return:
        a list of tokens with white space removed at the start and end,
        but not in the middle.
    :raises:
        :class:`ParseError` if there is any invalid token for the 'value'
        production of the core grammar.

    """
    content = []
    for token in tokens:
        type_ = token.type
        # Skip white space at the start
        if content or type_ != 'S':
            if type_ == '{':
                validate_block(token, 'property value')
            else:
                validate_any(token, 'property value')
            content.append(token)

    # Remove white space at the end
    while content and content[-1].type == 'S':
        content.pop()
    return content


def validate_block(tokens, context):
    """
    :raises:
        :class:`ParseError` if there is any invalid token for the 'block'
        production of the core grammar.
    :param tokens: an iterable of tokens
    :param context: a string for the 'unexpected in ...' message

    """
    for token in tokens:
        type_ = token.type
        if type_ == '{':
            validate_block(token.value, context)
        elif type_ not in (';', 'ATKEYWORD'):
            validate_any(token, context)


def validate_any(token, context):
    """
    :raises:
        :class:`ParseError` if this is an invalid token for the
        'any' production of the core grammar.
    :param token: a single token
    :param context: a string for the 'unexpected in ...' message

    """
    type_ = token.type
    if type_ in ('FUNCTION', '(', '['):
        for token in token.content:
            validate_any(token, type_)
    elif type_ not in ('S', 'IDENT', 'DIMENSION', 'PERCENTAGE', 'NUMBER',
                       'URI', 'DELIM', 'STRING', 'HASH', 'ATKEYWORD', ':',
                       'UNICODE-RANGE', 'INCLUDES', 'DASHMATCH'):
        raise UnexpectedToken(error_token, context)


if __name__ == '__main__':
    # XXX debug
    import sys, pprint
    with open(sys.argv[1], 'rb') as fd:
        content = fd.read().decode('utf8')
    rules, errors = parse(content)
    print(len(rules), len(errors))
    for at, head, body in rules:
        print(at)
        if at:
            for v in head:
                print (v.pretty())
            print (body.pretty())
        else:
            print (head.pretty())
            for n, v in body:
                print(n)
                for vv in v:
                    print(vv.pretty())
    for e in errors:
        print(e)
