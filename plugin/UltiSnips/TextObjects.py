#!/usr/bin/env python
# encoding: utf-8

import os
import re
import stat
import tempfile
import vim

from UltiSnips.Util import IndentUtil
from UltiSnips.Buffer import TextBuffer
from UltiSnips.Geometry import Span, Position

__all__ = [ "Mirror", "Transformation", "SnippetInstance", "StartMarker" ]

from itertools import takewhile

from debug import debug

###########################################################################
#                              Helper class                               #
###########################################################################
class _CleverReplace(object):
    """
    This class mimics TextMates replace syntax
    """
    _DOLLAR = re.compile(r"\$(\d+)", re.DOTALL)
    _SIMPLE_CASEFOLDINGS = re.compile(r"\\([ul].)", re.DOTALL)
    _LONG_CASEFOLDINGS = re.compile(r"\\([UL].*?)\\E", re.DOTALL)
    _CONDITIONAL = re.compile(r"\(\?(\d+):", re.DOTALL)

    _UNESCAPE = re.compile(r'\\[^ntrab]')

    def __init__(self, s):
        self._s = s

    def _scase_folding(self, m):
        if m.group(1)[0] == 'u':
            return m.group(1)[-1].upper()
        else:
            return m.group(1)[-1].lower()
    def _lcase_folding(self, m):
        if m.group(1)[0] == 'U':
            return m.group(1)[1:].upper()
        else:
            return m.group(1)[1:].lower()

    def _replace_conditional(self, match, v):
        def _find_closingbrace(v,start_pos):
            bracks_open = 1
            for idx, c in enumerate(v[start_pos:]):
                if c == '(':
                    if v[idx+start_pos-1] != '\\':
                        bracks_open += 1
                elif c == ')':
                    if v[idx+start_pos-1] != '\\':
                        bracks_open -= 1
                    if not bracks_open:
                        return start_pos+idx+1
        m = self._CONDITIONAL.search(v)

        def _part_conditional(v):
            bracks_open = 0
            args = []
            carg = ""
            for idx, c in enumerate(v):
                if c == '(':
                    if v[idx-1] != '\\':
                        bracks_open += 1
                elif c == ')':
                    if v[idx-1] != '\\':
                        bracks_open -= 1
                elif c == ':' and not bracks_open and not v[idx-1] == '\\':
                    args.append(carg)
                    carg = ""
                    continue
                carg += c
            args.append(carg)
            return args

        while m:
            start = m.start()
            end = _find_closingbrace(v,start+4)

            args = _part_conditional(v[start+4:end-1])

            rv = ""
            if match.group(int(m.group(1))):
                rv = self._unescape(self._replace_conditional(match,args[0]))
            elif len(args) > 1:
                rv = self._unescape(self._replace_conditional(match,args[1]))

            v = v[:start] + rv + v[end:]

            m = self._CONDITIONAL.search(v)
        return v

    def _unescape(self, v):
        return self._UNESCAPE.subn(lambda m: m.group(0)[-1], v)[0]
    def replace(self, match):
        start, end = match.span()

        tv = self._s

        # Replace all $? with capture groups
        tv = self._DOLLAR.subn(lambda m: match.group(int(m.group(1))), tv)[0]

        # Replace CaseFoldings
        tv = self._SIMPLE_CASEFOLDINGS.subn(self._scase_folding, tv)[0]
        tv = self._LONG_CASEFOLDINGS.subn(self._lcase_folding, tv)[0]
        tv = self._replace_conditional(match, tv)

        return self._unescape(tv.decode("string-escape"))

class _TOParser(object):
    # A simple tabstop with default value
    _TABSTOP = re.compile(r'''(?<![^\\]\\)\${(\d+)[:}]''')
    # A mirror or a tabstop without default value.
    _MIRROR_OR_TS = re.compile(r'(?<![^\\]\\)\$(\d+)')
    # A mirror or a tabstop without default value.
    _TRANSFORMATION = re.compile(r'(?<![^\\]\\)\${(\d+)/(.*?)/(.*?)/([a-zA-z]*)}')
    # The beginning of a shell code fragment
    _SHELLCODE = re.compile(r'(?<![^\\]\\)`')
    # The beginning of a python code fragment
    _PYTHONCODE = re.compile(r'(?<![^\\]\\)`!p')
    # The beginning of a vimL code fragment
    _VIMCODE = re.compile(r'(?<![^\\]\\)`!v')
    # Escaped characters in substrings
    _UNESCAPE = re.compile(r'\\[`$\\]')

    def __init__(self, parent, val, indent):


        self._v = val
        self._p = parent
        self._indent = indent

        self._childs = []

    def __repr__(self):
        return "TOParser(%s)" % self._p

    def parse(self):
        val = self._v
        # self._v = ""
        s = SnippetParser(self._p, val)
        s.parse(self._indent)

        # text = ""

        # idx = 0
        # while idx < len(self._v):
            # if self._v[idx] == '\\':
                # text += self._v[idx+1]
                # idx += 2
            # elif self._v[idx:].startswith("${"):
                # didx, dtext = self._parse_tabstop(idx, self._v)
                # debug("%r, %r" %(didx,dtext))
                # idx += didx
                # text += dtext
            # else:
                # text += self._v[idx]
                # idx += 1

        # self._v = text

        # self._parse_tabs()
        # self._parse_pythoncode()
        # self._parse_vimlcode()
        #
        # self._parse_shellcode()
        # self._parse_transformations()
        # self._parse_mirrors_or_ts()

        # self._parse_escaped_chars()

        # self._finish()

    #################
    # Escaped chars #
    #################
    def _parse_escaped_chars(self):
        m = self._UNESCAPE.search(self._v)
        while m:
            self._handle_unescape(m)
            m = self._UNESCAPE.search(self._v)

        for c in self._childs:
            c._parse_escaped_chars()

    def _handle_unescape(self, m):
        start_pos = m.start()
        end_pos = start_pos + 2
        char = self._v[start_pos+1]

        start, end = self._get_start_end(self._v,start_pos,end_pos)

        self._overwrite_area(start_pos,end_pos)

        return EscapedChar(self._p, start, end, char)

    ##############
    # Shell Code #
    ##############
    def _parse_shellcode(self):
        m = self._SHELLCODE.search(self._v)
        while m:
            self._handle_shellcode(m)
            m = self._SHELLCODE.search(self._v)

        for c in self._childs:
            c._parse_shellcode()

    def _handle_shellcode(self, m):
        start_pos = m.start()
        end_pos = self._find_closing_bt(start_pos+1)

        content = self._v[start_pos+1:end_pos-1]

        start, end = self._get_start_end(self._v,start_pos,end_pos)

        self._overwrite_area(start_pos,end_pos)

        return ShellCode(self._p, start, end, content)

    ###############
    # Python Code #
    ###############
    def _parse_pythoncode(self):
        m = self._PYTHONCODE.search(self._v)
        while m:
            self._handle_pythoncode(m)
            m = self._PYTHONCODE.search(self._v)

        for c in self._childs:
            c._parse_pythoncode()

    def _handle_pythoncode(self, m):
        start_pos = m.start()
        end_pos = self._find_closing_bt(start_pos+1)

        # Strip `!p `
        content = self._v[start_pos+3:end_pos-1]

        start, end = self._get_start_end(self._v,start_pos,end_pos)

        self._overwrite_area(start_pos,end_pos)

        # Strip the indent if any
        if len(self._indent):
            lines = content.splitlines()
            new_content = lines[0] + '\n'
            new_content += '\n'.join([l[len(self._indent):]
                        for l in lines[1:]])
        else:
            new_content = content
        new_content = new_content.strip()

        return PythonCode(self._p, start, end, new_content, self._indent)

    #############
    # VimL Code #
    #############
    def _parse_vimlcode(self):
        m = self._VIMCODE.search(self._v)
        while m:
            self._handle_vimlcode(m)
            m = self._VIMCODE.search(self._v)

        for c in self._childs:
            c._parse_vimlcode()

    def _handle_vimlcode(self, m):
        start_pos = m.start()
        end_pos = self._find_closing_bt(start_pos+1)

        # Strip `!v `
        content = self._v[start_pos+3:end_pos-1]

        start, end = self._get_start_end(self._v,start_pos,end_pos)

        self._overwrite_area(start_pos,end_pos)

        return VimLCode(self._p, start, end, content)

    ########
    # TABS #
    ########
    def _parse_tabstop(self, start_pos, v):
        def _find_closingbracket(v,start_pos):
            bracks_open = 1
            for idx, c in enumerate(v[start_pos:]):
                if c == '{':
                    if v[idx+start_pos-1] != '\\':
                        bracks_open += 1
                elif c == '}':
                    if v[idx+start_pos-1] != '\\':
                        bracks_open -= 1
                    if not bracks_open:
                        return start_pos+idx+1

        end_pos = _find_closingbracket(self._v, start_pos+2)

        m = self._TABSTOP.match(v[start_pos:])
        def_text = self._v[m.end():end_pos-1]

        start, end = self._get_start_end(self._v,start_pos,end_pos)

        no = int(m.group(1))
        ts = TabStop(no, self._p, start, end, def_text)

        self._p._add_tabstop(no,ts)

        self._overwrite_area(start_pos, end_pos)

        return end_pos-start_pos, def_text
        ts = []
        # m = self._TABSTOP.search(self._v)

        # while m:
            # ts.append(self._handle_tabstop(m))
            # m = self._TABSTOP.search(self._v)

        # for t, def_text in ts:
            # child_parser = _TOParser(t, def_text, self._indent)
            # child_parser._parse_tabs()
            # self._childs.append(child_parser)


    ###################
    # TRANSFORMATIONS #
    ###################
    def _parse_transformations(self):
        self._trans = []
        for m in self._TRANSFORMATION.finditer(self._v):
            self._trans.append(self._handle_transformation(m))

        for t in self._childs:
            t._parse_transformations()

    def _handle_transformation(self, m):
        no = int(m.group(1))
        search = m.group(2)
        replace = m.group(3)
        options = m.group(4)

        start_pos, end_pos = m.span()
        start, end = self._get_start_end(self._v,start_pos,end_pos)

        self._overwrite_area(*m.span())

        return Transformation(self._p, no, start, end, search, replace, options)

    #####################
    # MIRRORS OR TS: $1 #
    #####################
    def _parse_mirrors_or_ts(self):
        for m in self._MIRROR_OR_TS.finditer(self._v):
            self._handle_ts_or_mirror(m)

        for t in self._childs:
            t._parse_mirrors_or_ts()

    def _handle_ts_or_mirror(self, m):
        no = int(m.group(1))

        start_pos, end_pos = m.span()
        start, end = self._get_start_end(self._v,start_pos,end_pos)

        ts = self._p._get_tabstop(self._p, no)
        if ts is not None:
            rv = Mirror(self._p, ts, start, end)
        else:
            rv = TabStop(no, self._p, start, end)
            self._p._add_tabstop(no,rv)

        self._overwrite_area(*m.span())

        return rv

    ###################
    # Resolve symbols #
    ###################
    def _finish(self):
        for c in self._childs:
            c._finish()

        for t in self._trans:
            ts = self._p._get_tabstop(self._p,t._ts)
            if ts is None:
                raise RuntimeError, "Tabstop %i is not known" % t._ts
            t._ts = ts


    ####################
    # Helper functions #
    ####################
    def _find_closing_bt(self, start_pos):
        for idx,c in enumerate(self._v[start_pos:]):
            if c == '`' and self._v[idx+start_pos-1] != '\\':
                return idx + start_pos + 1

    def _get_start_end(self, val, start_pos, end_pos):
        def _get_pos(s, pos):
            line_idx = s[:pos].count('\n')
            line_start = s[:pos].rfind('\n') + 1
            start_in_line = pos - line_start
            return Position(line_idx, start_in_line)

        return _get_pos(val, start_pos), _get_pos(val, end_pos)

    def _overwrite_area(self, s, e):
        """Overwrite the given span with spaces. But keep newlines in place"""
        area = self._v[s:e]
        area = '\n'.join( [" "*len(i) for i in area.splitlines()] )
        self._v = self._v[:s] + area + self._v[e:]



###########################################################################
#                             Public classes                              #
###########################################################################

class TextObject(object):
    """
    This base class represents any object in the text
    that has a span in any ways
    """
    def __init__(self, parent, start, end, initial_text):
        self._start = start
        self._end = end

        self._parent = parent

        self._childs = []
        self._tabstops = {}

        if parent is not None:
            parent._add_child(self)

        self._current_text = TextBuffer(initial_text)

        self._cts = 0

    def __cmp__(self, other):
        return cmp(self._start, other._start)


    ##############
    # PROPERTIES #
    ##############
    def current_text():
        def fget(self):
            return str(self._current_text)
        def fset(self, text):
            self._current_text = TextBuffer(text)

            # All our childs are set to "" so they
            # do no longer disturb anything that mirrors it
            for c in self._childs:
                c.current_text = ""
            self._childs = []
            self._tabstops = {}
        return locals()

    current_text = property(**current_text())
    def abs_start(self):
        if self._parent:
            ps = self._parent.abs_start
            if self._start.line == 0:
                return ps + self._start
            else:
                return Position(ps.line + self._start.line, self._start.col)
        return self._start
    abs_start = property(abs_start)

    def abs_end(self):
        if self._parent:
            ps = self._parent.abs_start
            if self._end.line == 0:
                return ps + self._end
            else:
                return Position(ps.line + self._end.line, self._end.col)

        return self._end
    abs_end = property(abs_end)

    def span(self):
        return Span(self._start, self._end)
    span = property(span)

    def start(self):
        return self._start
    start = property(start)

    def end(self):
        return self._end
    end = property(end)

    def abs_span(self):
        return Span(self.abs_start, self.abs_end)
    abs_span = property(abs_span)

    ####################
    # Public functions #
    ####################
    def update(self):
        for idx,c in enumerate(self._childs):
            oldend = Position(c.end.line, c.end.col)

            new_end = c.update()

            moved_lines = new_end.line - oldend.line
            moved_cols = new_end.col - oldend.col

            self._current_text.replace_text(c.start, oldend, c._current_text)

            self._move_textobjects_behind(c.start, oldend, moved_lines,
                        moved_cols, idx)

        self._do_update()

        new_end = self._current_text.calc_end(self._start)

        self._end = new_end

        return new_end

    def _get_next_tab(self, no):
        if not len(self._tabstops.keys()):
            return
        tno_max = max(self._tabstops.keys())

        posible_sol = []
        i = no + 1
        while i <= tno_max:
            if i in self._tabstops:
                posible_sol.append( (i, self._tabstops[i]) )
                break
            i += 1

        c = [ c._get_next_tab(no) for c in self._childs ]
        c = filter(lambda i: i, c)

        posible_sol += c

        if not len(posible_sol):
            return None

        return min(posible_sol)


    def _get_prev_tab(self, no):
        if not len(self._tabstops.keys()):
            return
        tno_min = min(self._tabstops.keys())

        posible_sol = []
        i = no - 1
        while i >= tno_min and i > 0:
            if i in self._tabstops:
                posible_sol.append( (i, self._tabstops[i]) )
                break
            i -= 1

        c = [ c._get_prev_tab(no) for c in self._childs ]
        c = filter(lambda i: i, c)

        posible_sol += c

        if not len(posible_sol):
            return None

        return max(posible_sol)


    ###############################
    # Private/Protected functions #
    ###############################
    def _do_update(self):
        pass

    def _move_textobjects_behind(self, start, end, lines, cols, obj_idx):
        if lines == 0 and cols == 0:
            return

        for idx,m in enumerate(self._childs[obj_idx+1:]):
            delta_lines = 0
            delta_cols_begin = 0
            delta_cols_end = 0

            if m.start.line > end.line:
                delta_lines = lines
            elif m.start.line == end.line:
                if m.start.col >= end.col:
                    if lines:
                        delta_lines = lines
                    delta_cols_begin = cols
                    if m.start.line == m.end.line:
                        delta_cols_end = cols
            m.start.line += delta_lines
            m.end.line += delta_lines
            m.start.col += delta_cols_begin
            m.end.col += delta_cols_end

    def _get_tabstop(self, requester, no):
        if no in self._tabstops:
            return self._tabstops[no]
        for c in self._childs:
            if c is requester:
                continue

            rv = c._get_tabstop(self, no)
            if rv is not None:
                return rv
        if self._parent and requester is not self._parent:
            return self._parent._get_tabstop(self, no)

    def _add_child(self,c):
        self._childs.append(c)
        self._childs.sort()

    def _add_tabstop(self, no, ts):
        self._tabstops[no] = ts

class EscapedChar(TextObject):
    """
    This class is a escape char like \$. It is handled in a text object
    to make sure that remaining children are correctly moved after
    replacing the text.

    This is a base class without functionality just to mark it in the code.
    """
    pass


class StartMarker(TextObject):
    """
    This class only remembers it's starting position. It is used to
    transform relative values into absolute position values in the vim
    buffer
    """
    def __init__(self, start):
        end = Position(start.line, start.col)
        TextObject.__init__(self, None, start, end, "")


class Mirror(TextObject):
    """
    A Mirror object mirrors a TabStop that is, text is repeated here
    """
    def __init__(self, parent, ts, start, end):
        TextObject.__init__(self, parent, start, end, "")

        self._ts = ts

    def _do_update(self):
        self.current_text = self._ts.current_text

    def __repr__(self):
        return "Mirror(%s -> %s)" % (self._start, self._end)


class Transformation(Mirror):
    def __init__(self, parent, ts, start, end, s, r, options):
        Mirror.__init__(self, parent, ts, start, end)

        flags = 0
        self._match_this_many = 1
        if options:
            if "g" in options:
                self._match_this_many = 0
            if "i" in options:
                flags |=  re.IGNORECASE

        self._find = re.compile(s, flags | re.DOTALL)
        self._replace = _CleverReplace(r)

    def _do_update(self):
        t = self._ts.current_text
        t = self._find.subn(self._replace.replace, t, self._match_this_many)[0]
        self.current_text = t

    def __repr__(self):
        return "Transformation(%s -> %s)" % (self._start, self._end)

class ShellCode(TextObject):
    def __init__(self, parent, start, end, code):

        code = code.replace("\\`", "`")

        # Write the code to a temporary file
        handle, path = tempfile.mkstemp(text=True)
        os.write(handle, code)
        os.close(handle)

        os.chmod(path, stat.S_IRWXU)

        # Interpolate the shell code. We try to stay as compatible with Python
        # 2.3, therefore, we do not use the subprocess module here
        output = os.popen(path, "r").read()
        if len(output) and output[-1] == '\n':
            output = output[:-1]
        if len(output) and output[-1] == '\r':
            output = output[:-1]

        os.unlink(path)

        TextObject.__init__(self, parent, start, end, output)

    def __repr__(self):
        return "ShellCode(%s -> %s)" % (self._start, self._end)

class VimLCode(TextObject):
    def __init__(self, parent, start, end, code):
        self._code = code.replace("\\`", "`").strip()

        TextObject.__init__(self, parent, start, end, "")

    def _do_update(self):
        self.current_text = str(vim.eval(self._code))

    def __repr__(self):
        return "VimLCode(%s -> %s)" % (self._start, self._end)

class _Tabs(object):
    def __init__(self, to):
        self._to = to

    def __getitem__(self, no):
        ts = self._to._get_tabstop(self._to, int(no))
        if ts is None:
            return ""
        return ts.current_text

class SnippetUtil(object):
    """ Provides easy access to indentation, etc.
    """

    def __init__(self, initial_indent, cur=""):
        self._ind = IndentUtil()

        self._initial_indent = self._ind.indent_to_spaces(initial_indent)

        self._reset(cur)

    def _reset(self, cur):
        """ Gets the snippet ready for another update.

        :cur: the new value for c.
        """
        self._ind.reset()
        self._c = cur
        self._rv = ""
        self._changed = False
        self.reset_indent()

    def shift(self, amount=1):
        """ Shifts the indentation level.
        Note that this uses the shiftwidth because thats what code
        formatters use.

        :amount: the amount by which to shift.
        """
        self.indent += " " * self._ind.sw * amount

    def unshift(self, amount=1):
        """ Unshift the indentation level.
        Note that this uses the shiftwidth because thats what code
        formatters use.

        :amount: the amount by which to unshift.
        """
        by = -self._ind.sw * amount
        try:
            self.indent = self.indent[:by]
        except IndexError:
            indent = ""

    def mkline(self, line="", indent=None):
        """ Creates a properly set up line.

        :line: the text to add
        :indent: the indentation to have at the beginning
                 if None, it uses the default amount
        """
        if indent == None:
            indent = self.indent
            # this deals with the fact that the first line is
            # already properly indented
            if '\n' not in self._rv:
                try:
                    indent = indent[len(self._initial_indent):]
                except IndexError:
                    indent = ""
            indent = self._ind.spaces_to_indent(indent)

        return indent + line

    def reset_indent(self):
        """ Clears the indentation. """
        self.indent = self._initial_indent

    # Utility methods
    @property
    def fn(self):
        """ The filename. """
        return vim.eval('expand("%:t")') or ""

    @property
    def basename(self):
        """ The filename without extension. """
        return vim.eval('expand("%:t:r")') or ""

    @property
    def ft(self):
        """ The filetype. """
        return self.opt("&filetype", "")

    # Necessary stuff
    def rv():
        """ The return value.
        This is a list of lines to insert at the
        location of the placeholder.

        Deprecates res.
        """
        def fget(self):
            return self._rv
        def fset(self, value):
            self._changed = True
            self._rv = value
        return locals()
    rv = property(**rv())

    @property
    def _rv_changed(self):
        """ True if rv has changed. """
        return self._changed

    @property
    def c(self):
        """ The current text of the placeholder.

        Deprecates cur.
        """
        return self._c

    def opt(self, option, default=None):
        """ Gets a vim variable. """
        if vim.eval("exists('%s')" % option) == "1":
            try:
                return vim.eval(option)
            except vim.error:
                pass
        return default

    # Syntatic sugar
    def __add__(self, value):
        """ Appends the given line to rv using mkline. """
        self.rv += '\n' # handles the first line properly
        self.rv += self.mkline(value)
        return self

    def __lshift__(self, other):
        """ Same as unshift. """
        self.unshift(other)

    def __rshift__(self, other):
        """ Same as shift. """
        self.shift(other)


class PythonCode(TextObject):
    def __init__(self, parent, start, end, code, indent=""):

        code = code.replace("\\`", "`")

        # Find our containing snippet for snippet local data
        snippet = parent
        while snippet and not isinstance(snippet, SnippetInstance):
            try:
                snippet = snippet._parent
            except AttributeError:
                snippet = None
        self._snip = SnippetUtil(indent)
        self._locals = snippet.locals

        self._globals = {}
        globals = snippet.globals.get("!p", [])
        exec "\n".join(globals).replace("\r\n", "\n") in self._globals

        # Add Some convenience to the code
        self._code = "import re, os, vim, string, random\n" + code

        TextObject.__init__(self, parent, start, end, "")


    def _do_update(self):
        path = vim.eval('expand("%")')
        if path is None:
            path = ""
        fn = os.path.basename(path)

        ct = self.current_text
        self._snip._reset(ct)
        local_d = self._locals

        local_d.update({
            't': _Tabs(self),
            'fn': fn,
            'path': path,
            'cur': ct,
            'res': ct,
            'snip' : self._snip,
        })

        self._code = self._code.replace("\r\n", "\n")
        exec self._code in self._globals, local_d

        if self._snip._rv_changed:
            self.current_text = self._snip.rv
        else:
            self.current_text = str(local_d["res"])

    def __repr__(self):
        return "PythonCode(%s -> %s)" % (self._start, self._end)

class TabStop(TextObject):
    """
    This is the most important TextObject. A TabStop is were the cursor
    comes to rest when the user taps through the Snippet.
    """
    def __init__(self, no, parent, start, end, default_text = ""):
        TextObject.__init__(self, parent, start, end, default_text)
        self._no = no

    def no(self):
        return self._no
    no = property(no)

    def __repr__(self):
        return "TabStop(%s -> %s, %s)" % (self._start, self._end,
            repr(self._current_text))

class SnippetInstance(TextObject):
    """
    A Snippet instance is an instance of a Snippet Definition. That is,
    when the user expands a snippet, a SnippetInstance is created to
    keep track of the corresponding TextObjects. The Snippet itself is
    also a TextObject because it has a start an end
    """

    def __init__(self, parent, indent, initial_text, start = None, end = None, last_re = None, globals = None):
        if start is None:
            start = Position(0,0)
        if end is None:
            end = Position(0,0)

        self.locals = {"match" : last_re}
        self.globals = globals

        TextObject.__init__(self, parent, start, end, initial_text)

        _TOParser(self, initial_text, indent).parse()

        # Check if we have a zero Tab, if not, add one at the end
        if isinstance(parent, TabStop):
            if not parent.no == 0:
                # We are recursively called, if we have a zero tab, remove it.
                if 0 in self._tabstops:
                    self._tabstops[0].current_text = ""
                    del self._tabstops[0]
        else:
            self.update()
            if 0 not in self._tabstops:
                delta = self._end - self._start
                col = self.end.col
                if delta.line == 0:
                    col -= self.start.col
                start = Position(delta.line, col)
                end = Position(delta.line, col)
                ts = TabStop(0, self, start, end, "")
                self._add_tabstop(0,ts)

                self.update()

    def __repr__(self):
        return "SnippetInstance(%s -> %s)" % (self._start, self._end)

    def has_tabs(self):
        return len(self._tabstops)
    has_tabs = property(has_tabs)

    def _get_tabstop(self, requester, no):
        # SnippetInstances are completely self contained, therefore, we do not
        # need to ask our parent for Tabstops
        p = self._parent
        self._parent = None
        rv = TextObject._get_tabstop(self, requester, no)
        self._parent = p

        return rv

    def select_next_tab(self, backwards = False):
        if self._cts is None:
            return

        if backwards:
            cts_bf = self._cts

            res = self._get_prev_tab(self._cts)
            if res is None:
                self._cts = cts_bf
                return self._tabstops[self._cts]
            self._cts, ts = res
            return ts
        else:
            res = self._get_next_tab(self._cts)
            if res is None:
                self._cts = None
                if 0 in self._tabstops:
                    return self._tabstops[0]
                else:
                    return None
            else:
                self._cts, ts = res
                return ts

        return self._tabstops[self._cts]

## TODO: everything below here should be it's own module
from debug import debug
import string

class TextIterator(object):
    def __init__(self, text):
        self._text = text
        self._line = 0
        self._col = 0

        self._idx = 0

    def __iter__(self):
        return self

    def next(self):
        if self._idx >= len(self._text):
            raise StopIteration

        rv = self._text[self._idx]
        if self._text[self._idx] in ('\n', '\r\n'):
            self._line += 1
            self._col = 0
        else:
            self._col += 1
        self._idx += 1

        return rv

    def peek(self, count = 1):
        try:
            return self._text[self._idx:self._idx + count]
        except IndexError:
            return None

    @property
    def idx(self):
        return self._idx # TODO: does this need to be exposed?

    @property
    def pos(self):
        return Position(self._line, self._col)

    @property
    def exhausted(self):
        return self._idx >= len(self._text)

class Token(object):
    def __init__(self, gen, indent):
        self.start = gen.pos
        self._parse(gen, indent)
        self.end = gen.pos


def _parse_number(stream):
    # TODO: document me
    rv = ""
    while stream.peek() in string.digits:
        rv += stream.next()

    return int(rv)

def _parse_till_closing_brace(stream):
    # TODO: document me, this also eats the closing brace
    rv = ""
    in_braces = 1
    while True:
        if EscapeCharToken.check(stream, '{}'):
            rv += stream.next() + stream.next()
        else:
            c = stream.next()
            if c == '{': in_braces += 1
            elif c == '}': in_braces -= 1
            if in_braces == 0:
                break
            rv += c
    return rv


# TODO: the functionality of some of these functions are quite
# similar. Somekind of next_matching
def _parse_till_unescaped_char(stream, char):
    # TODO: document me, this also eats the closing slash
    rv = ""
    in_braces = 1
    while True:
        if EscapeCharToken.check(stream, char):
            rv += stream.next() + stream.next()
        else:
            c = stream.next()
            if c == char:
                break
            rv += c
    return rv

class TabStopToken(Token):
    CHECK = re.compile(r'^\${\d+[:}]')

    @classmethod
    def check(klass, stream):
        # TODO: bad name for function
        return klass.CHECK.match(stream.peek(10)) != None

    def _parse(self, stream, indent):
        stream.next() # $
        stream.next() # {

        self.no = _parse_number(stream)

        if stream.peek() is ":":
            stream.next()
        self.default_text = _parse_till_closing_brace(stream)
        debug("self.start: %s, stream.pos: %s" % (self.start, stream.pos))

    def __repr__(self):
        return "TabStopToken(%r,%r,%r,%r)" % (
            self.start, self.end, self.no, self.default_text
        )

class TransformationToken(Token):
    CHECK = re.compile(r'^\${\d+\/')

    @classmethod
    def check(klass, stream):
        # TODO: bad name for function
        return klass.CHECK.match(stream.peek(10)) != None

    def _parse(self, stream, indent):
        stream.next() # $
        stream.next() # {

        self.no = _parse_number(stream)

        stream.next() # /

        self.search = _parse_till_unescaped_char(stream, '/')
        self.replace = _parse_till_unescaped_char(stream, '/')
        self.options = _parse_till_closing_brace(stream)

    def __repr__(self):
        return "TransformationToken(%r,%r,%r,%r,%r)" % (
            self.start, self.end, self.no, self.search, self.replace
        )

class MirrorToken(Token):
    CHECK = re.compile(r'^\$\d+')

    @classmethod
    def check(klass, stream):
        # TODO: bad name for function
        return klass.CHECK.match(stream.peek(10)) != None

    def _parse(self, stream, indent):
        self.no = ""
        stream.next() # $
        while not stream.exhausted and stream.peek() in string.digits:
            self.no += stream.next()
        self.no = int(self.no)

    def __repr__(self):
        return "MirrorToken(%r,%r,%r)" % (
            self.start, self.end, self.no
        )

class EscapeCharToken(Token):
    @classmethod
    def check(klass, stream, chars = '{}\$`'):
        cs = stream.peek(2)
        if len(cs) == 2 and cs[0] == '\\' and cs[1] in chars:
            return True

    def _parse(self, stream, indent):
        stream.next() # \
        self.char = stream.next()


    # TODO: get rid of those __repr__ maybe
    def __repr__(self):
        return "EscapeCharToken(%r,%r,%r)" % (
            self.start, self.end, self.char
        )

class ShellCodeToken(Token):
    @classmethod
    def check(klass, stream):
        return stream.peek(1) == '`'

    def _parse(self, stream, indent):
        stream.next() # `
        self.content = _parse_till_unescaped_char(stream, '`')

    # TODO: get rid of those __repr__ maybe
    def __repr__(self):
        return "ShellCodeToken(%r,%r,%r)" % (
            self.start, self.end, self.content
        )


# TODO: identical to VimLCodeToken
class PythonCodeToken(Token):
    CHECK = re.compile(r'^`!p\s')

    @classmethod
    def check(klass, stream):
        return klass.CHECK.match(stream.peek(4)) is not None

    def _parse(self, stream, indent):
        for i in range(3):
            stream.next() # `!p
        if stream.peek() in '\t ':
            stream.next()

        content = _parse_till_unescaped_char(stream, '`')

        # TODO: stupid to pass the indent down even if only python
        # needs it. Stupid to indent beforehand.

        debug("indent: %r" % (indent))
        # Strip the indent if any
        if len(indent):
            lines = content.splitlines()
            self.content = lines[0] + '\n'
            self.content += '\n'.join([l[len(indent):]
                        for l in lines[1:]])
        else:
            self.content = content
        self.indent = indent

    # TODO: get rid of those __repr__ maybe
    def __repr__(self):
        return "PythonCodeToken(%r,%r,%r)" % (
            self.start, self.end, self.content
        )


class VimLCodeToken(Token):
    CHECK = re.compile(r'^`!v\s')

    @classmethod
    def check(klass, stream):
        return klass.CHECK.match(stream.peek(4)) is not None

    def _parse(self, stream, indent):
        for i in range(4):
            stream.next() # `!v
        self.content = _parse_till_unescaped_char(stream, '`')

    # TODO: get rid of those __repr__ maybe
    def __repr__(self):
        return "VimLCodeToken(%r,%r,%r)" % (
            self.start, self.end, self.content
        )

class ParsingMode(object):
    def tokens(self, stream, indent):
        while True:
            done_something = False
            for t in self.ALLOWED_TOKENS:
                if t.check(stream):
                    yield t(stream, indent)
                    done_something = True
                    break
            if not done_something:
                stream.next()

class LiteralMode(ParsingMode):
    ALLOWED_TOKENS = [ EscapeCharToken, TransformationToken, TabStopToken, MirrorToken, PythonCodeToken, VimLCodeToken, ShellCodeToken ]


class SnippetParser(object):
    def __init__(self, parent, text):
        debug("text: %s" % (text))
        self.current_to = parent
        self.stream = TextIterator(text)
        self.mode = None


    def parse(self, indent):

        seen_ts = {}
        dangling_references = set()
        tokens = []

        self._parse(indent, tokens, seen_ts, dangling_references)

        debug("all tokens: %s" % (tokens))
        debug("seen_ts: %s" % (seen_ts))
        debug("dangling_references: %s" % (dangling_references))
        # TODO: begin second phase: resolve ambiguity
        # TODO: do this only once at the top level
        for parent, token in tokens:
            if isinstance(token, MirrorToken):
                # TODO: maybe we can get rid of _get_tabstop and _add_tabstop
                if token.no not in seen_ts:
                    debug("token.start: %s, token.end: %s" % (token.start, token.end))
                    ts = TabStop(token.no, parent, token.start, token.end)
                    seen_ts[token.no] = ts
                    parent._add_tabstop(token.no,ts)
                else:
                    Mirror(parent, seen_ts[token.no], token.start, token.end)

        # TODO: third phase: associate tabstops with Transformations
        # TODO: do this only once
        # TODO: this access private parts
        resolved_ts = set()
        for tr in dangling_references:
            if tr._ts in seen_ts:
                tr._ts = seen_ts[tr._ts]
                resolved_ts.add(tr)

        # TODO: check if all associations have been done properly. Also add a testcase for this!
        dangling_references -= resolved_ts

    def _parse(self, indent, all_tokens, seen_ts, dangling_references):
        tokens = list(LiteralMode().tokens(self.stream, indent))

        debug("tokens: %s" % (tokens))
        for token in tokens:
            all_tokens.append((self.current_to, token))

            if isinstance(token, TabStopToken):
                # TODO: could also take the token directly
                debug("token.start: %s, token.end: %s" % (token.start, token.end))
                ts = TabStop(token.no, self.current_to,
                        token.start, token.end, token.default_text)
                seen_ts[token.no] = ts
                self.current_to._add_tabstop(token.no,ts)

                # TODO: can't parsing be done here directly?
                k = SnippetParser(ts, ts.current_text)
                k._parse(indent, all_tokens, seen_ts, dangling_references)
            elif isinstance(token, EscapeCharToken):
                EscapedChar(self.current_to, token.start, token.end, token.char)
            elif isinstance(token, TransformationToken):
                tr = Transformation(self.current_to, token.no, token.start, token.end, token.search, token.replace, token.options)
                dangling_references.add(tr)
            elif isinstance(token, ShellCodeToken):
                ShellCode(self.current_to, token.start, token.end, token.content)
            elif isinstance(token, PythonCodeToken):
                PythonCode(self.current_to, token.start, token.end, token.content, token.indent)
            elif isinstance(token, VimLCodeToken):
                VimLCode(self.current_to, token.start, token.end, token.content)

