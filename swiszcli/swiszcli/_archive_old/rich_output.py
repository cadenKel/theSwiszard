from rich.console import Console, Group
from rich.panel import Panel
from rich.box import ROUNDED
from rich.text import Text
from rich.table import Table
import shutil
import os as _os

_console = None


def _get_console():
    global _console
    if _console is None:
        # Always emit color — swiszCLI is only used interactively
        if 'FORCE_COLOR' not in _os.environ:
            _os.environ['FORCE_COLOR'] = '1'
        tw = shutil.get_terminal_size().columns
        _console = Console(width=min(tw, 100), highlight=False, force_terminal=True, markup=False)
    return _console


def print_banner(session_id, model_name, swiszard_path, mem_url):
    console = _get_console()
    home = _os.path.expanduser('~')
    if swiszard_path.startswith(home):
        swiszard_path = '~' + swiszard_path[len(home):]
    if mem_url.startswith('http://'):
        mem_url = mem_url[7:]
    if mem_url.startswith('127.0.0.1'):
        mem_url = ':' + mem_url.split(':')[-1]
    table = Table.grid(padding=(0, 1))
    table.add_column(justify='left')
    table.add_column(justify='right')
    table.add_row(Text('swiszCLI', style='bold cyan'), Text(model_name, style='dim magenta'))
    table.add_row(Text('session', style='dim'), Text(f'{session_id}  local ollama', style='dim'))
    right = Text.assemble(
        (swiszard_path, 'dim blue'),
        ('  ', 'dim'),
        ('swizmem', 'dim'),
        (' ', 'dim'),
        (mem_url, 'blue'),
    )
    table.add_row(Text('swiszard', style='dim'), right)
    footer = Text('  type /help for commands', style='dim')
    body = Group(table, footer)
    panel = Panel(body, box=ROUNDED, padding=(1, 2), title=Text('swiszCLI', style='bold cyan'), title_align='left')
    console.print(panel)


def print_tool_call(task, result, dt):
    console = _get_console()
    print()  # ensure newline before tool panel
    is_err = isinstance(result, str) and result.startswith('ERROR')
    body = result
    if isinstance(result, str) and result.startswith('handler_'):
        try:
            colon = result.index(':')
            body = result[colon + 1:].strip()
        except ValueError:
            pass
    lines = (body or '').splitlines()[:8] if isinstance(body, str) else []
    if len(lines) >= 8 and len(body) > 800:
        lines[-1] = '... (truncated)'
    body_text = "\n".join(lines)
    status = 'err' if is_err else 'ok'
    sc = 'red' if is_err else 'green'
    header = Text.assemble(
        ('── swisz ──\n', 'dim'),
        ('%s\n' % task[:160], 'dim magenta'),
    )
    parts = [header]
    if body_text:
        parts.append(Text(body_text + '\n', style='dim'))
    parts.append(Text.assemble(
        ('└─ %s' % status, 'bold %s' % sc),
        ('  %.2fs' % dt, 'dim'),
    ))
    body_group = Group(*parts)
    panel = Panel(body_group, box=ROUNDED, padding=(0, 1), expand=False)
    console.print(panel)


def print_section(title, body, dimmed=True):
    console = _get_console()
    print()  # ensure newline before section block
    lines = (body or '').splitlines()
    if dimmed:
        body_text = "\n".join(lines)
        body_node = Text(body_text, style='dim')
    else:
        body_node = Text(body or '')
    content = Group(
        Text(title, style='bold cyan'),
        body_node,
    )
    panel = Panel(content, box=ROUNDED, padding=(0, 1), expand=False)
    console.print(panel)


def print_assistant_prompt():
    _get_console().print(Text('caden▸ ', style='bold cyan'), end='')


def print_user_prompt():
    _get_console().print(Text('you▸ ', style='bold green'), end='')


def print_stats(chars=0, duration_ms=0, gen_toks=0, prompt_toks=0):
    tps = gen_toks * 1000 // duration_ms if duration_ms else 0
    total_toks = prompt_toks + gen_toks
    _get_console().print(Text(f'── {total_toks}tok  {tps}t/s  {duration_ms}ms', style='dim'))


def print_info(text, symbol='▸'):
    _get_console().print(Text(f'  {symbol} {text}', style='dim'))


def print_success(text):
    _get_console().print(Text(f'  ✓ {text}', style='green'))


def print_warning(text):
    _get_console().print(Text(f'  ⚠ {text}', style='yellow'))


def print_error(text):
    _get_console().print(Text(f'  ✗ {text}', style='red'))
