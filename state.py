import sublime
import sublime_plugin

import threading

from Vintageous.vi import actions
from Vintageous.vi import constants
from Vintageous.vi import motions
from Vintageous.vi import registers
from Vintageous.vi import utils
from Vintageous.vi.cmd_data import CmdData
from Vintageous.vi.constants import _MODE_INTERNAL_NORMAL
from Vintageous.vi.constants import ACTIONS_EXITING_TO_INSERT_MODE
from Vintageous.vi.constants import DIGRAPH_MOTION
from Vintageous.vi.constants import digraphs
from Vintageous.vi.constants import MODE_INSERT
from Vintageous.vi.constants import MODE_NORMAL
from Vintageous.vi.constants import MODE_NORMAL_INSERT
from Vintageous.vi.constants import MODE_REPLACE
from Vintageous.vi.constants import mode_to_str
from Vintageous.vi.constants import MODE_VISUAL
from Vintageous.vi.constants import MODE_VISUAL_LINE
from Vintageous.vi.contexts import KeyContext
from Vintageous.vi.registers import Registers
from Vintageous.vi.settings import SettingsManager
from Vintageous.vi.settings import SublimeSettings
from Vintageous.vi.settings import VintageSettings
from Vintageous.vi.marks import Marks


# Some commands use input panels to gather user input. When they call their .on_done() method,
# they most likely want the global state to remain untouched. However, an input panel is just
# a view, so when it closes, the previous view gets activated and Vintageous init code runs.
# This variable lets it know that is should exit early.
# XXX: Make this a class-level attribute of VintageState (had some trouble with it last time I tried).
# XXX Is there anything weird with ST and using class-level attributes from different modules?
_dont_reset_during_init = False


def _init_vintageous(view):
    global _dont_reset_during_init
    # Operate only on actual views.
    if (not getattr(view, 'settings') or
            view.settings().get('is_widget')):
        return

    if _dont_reset_during_init:
        # We are probably coming from an input panel, like when using '/'.
        _dont_reset_during_init = False
        return

    state = VintageState(view)

    if state.mode in (MODE_VISUAL, MODE_VISUAL_LINE):
        view.run_command('enter_normal_mode')
    elif state.mode in (MODE_INSERT, MODE_REPLACE):
        view.run_command('vi_enter_normal_mode_from_insert_mode')
    elif state.mode == MODE_NORMAL_INSERT:
        view.run_command('vi_run_normal_insert_mode_actions')
    else:
        # XXX: When is this run? Only at startup?
        state.enter_normal_mode()

    state.reset()


def plugin_loaded():
    view = sublime.active_window().active_view()
    _init_vintageous(view)


def unload_handler():
    for w in sublime.windows():
        for v in w.views():
            v.settings().set('command_mode', False)
            v.settings().set('inverse_caret_state', False)
            v.settings().set('vintage', {})


class VintageState(object):
    """ Stores per-view state using View.Settings() for storage.
    """

    # Makes yank data globally accesible.
    registers = Registers()
    context = KeyContext()
    marks = Marks()

    _latest_repeat_command = None

    def __init__(self, view):
        self.view = view
        # We have two types of settings: vi-specific (settings.vi) and regular
        # ST view settings (settings.view).
        self.settings = SettingsManager(self.view)

    def enter_normal_mode(self):
        self.settings.view['command_mode'] = True
        self.settings.view['inverse_caret_state'] = True
        # Make sure xpos is up to date when we return to normal mode. Insert mode does not affect
        # xpos.
        # XXX: Why is insert mode resetting xpos? xpos should never be reset?
        self.xpos = None if not self.view.sel() else self.view.rowcol(self.view.sel()[0].b)[1]
        self.mode = MODE_NORMAL

        if self.view.overwrite_status():
            self.view.set_overwrite_status(False)

        # Hide outlined regions created by searching.
        self.view.erase_regions('vi_search')

        self.view.run_command('glue_marked_undo_groups')

    def enter_visual_line_mode(self):
        self.mode = MODE_VISUAL_LINE

    def enter_insert_mode(self):
        self.settings.view['command_mode'] = False
        self.settings.view['inverse_caret_state'] = False
        self.mode = MODE_INSERT

    def enter_visual_mode(self):
        self.mode = MODE_VISUAL

    def enter_normal_insert_mode(self):
        self.mode = MODE_NORMAL_INSERT
        self.settings.view['command_mode'] = False
        self.settings.view['inverse_caret_state'] = False

    def enter_replace_mode(self):
        self.mode = MODE_REPLACE
        self.settings.view['command_mode'] = False
        self.settings.view['inverse_caret_state'] = False
        self.view.set_overwrite_status(True)

    def store_visual_selections(self):
        self.view.add_regions('vi_visual_selections', list(self.view.sel()))

    @property
    def mode(self):
        return self.settings.vi['mode']

    @mode.setter
    def mode(self, value):
        self.settings.vi['mode'] = value

    @property
    def cancel_action(self):
        # If we can't find a suitable action, we should cancel.
        return self.settings.vi['cancel_action']

    @cancel_action.setter
    def cancel_action(self, value):
        self.settings.vi['cancel_action'] = value

    @property
    def action(self):
        return self.settings.vi['action']

    @action.setter
    def action(self, name):
        action = self.settings.vi['action']
        target = 'action'

        # Check for digraphs like cc, dd, yy.
        if action and name:
            name, type_ = digraphs.get((action, name), ('', None))
            # Some motion digraphs are captured as actions, but need to be stored as motions
            # instead so that the vi command is evaluated correctly.
            if type_ == DIGRAPH_MOTION:
                target = 'motion'
                self.settings.vi['action'] = None

        # Avoid recursion. The .reset() method will try to set this property to None, not ''.
        if name == '':
            # The chord is invalid, so notify that we need to cancel the command in .run().
            self.cancel_action = True
            return

        self.settings.vi[target] = name

    @property
    def motion(self):
        return self.settings.vi['motion']

    @motion.setter
    def motion(self, name):
        self.settings.vi['motion'] = name

    @property
    def motion_digits(self):
        return self.settings.vi['motion_digits'] or []

    @motion_digits.setter
    def motion_digits(self, value):
        self.settings.vi['motion_digits'] = value

    def push_motion_digit(self, value):
        digits = self.settings.vi['motion_digits']
        if not digits:
            self.settings.vi['motion_digits'] = [value]
            return
        digits.append(value)
        self.settings.vi['motion_digits'] = digits

    @property
    def action_digits(self):
        return self.settings.vi['action_digits'] or []

    @action_digits.setter
    def action_digits(self, value):
        self.settings.vi['action_digits'] = value

    def push_action_digit(self, value):
        digits = self.settings.vi['action_digits']
        if not digits:
            self.settings.vi['action_digits'] = [value]
            return
        digits.append(value)
        self.settings.vi['action_digits'] = digits

    @property
    def count(self):
        """Computes and returns the final count, defaulting to 1 if the user
           didn't provide one.
        """
        motion_count = self.motion_digits and int(''.join(self.motion_digits)) or 1
        action_count = self.action_digits and int(''.join(self.action_digits)) or 1

        return (motion_count * action_count)

    @property
    def user_provided_count(self):
        """Returns the actual count provided by the user, which may be `None`.
        """
        if not (self.motion_digits or self.action_digits):
            return None

        return self.count

    @property
    def register(self):
        return self.settings.vi['register'] or None

    @property
    def expecting_register(self):
        return self.settings.vi['expecting_register']

    @expecting_register.setter
    def expecting_register(self, value):
        self.settings.vi['expecting_register'] = value

    @register.setter
    def register(self, name):
        # TODO: Check for valid register name.
        self.settings.vi['register'] = name
        self.expecting_register = False

    @property
    def expecting_user_input(self):
        return self.settings.vi['expecting_user_input']

    @expecting_user_input.setter
    def expecting_user_input(self, value):
        self.settings.vi['expecting_user_input'] = value

    @property
    def user_input(self):
        return self.settings.vi['user_input'] or None

    @user_input.setter
    def user_input(self, value):
        self.settings.vi['user_input'] = value
        self.expecting_user_input = False

    @property
    def last_buffer_search(self):
        return self.settings.vi['last_buffer_search'] or None

    @last_buffer_search.setter
    def last_buffer_search(self, value):
        self.settings.vi['last_buffer_search'] = value
        self.expecting_user_input = False

    @property
    def last_character_search(self):
        return self.settings.vi['last_character_search'] or None

    @last_character_search.setter
    def last_character_search(self, value):
        self.settings.vi['last_character_search'] = value
        self.expecting_user_input = False

    @property
    def xpos(self):
        xpos = self.settings.vi['xpos']
        return xpos if isinstance(xpos, int) else None

    @xpos.setter
    def xpos(self, value):
        self.settings.vi['xpos'] = value

    @property
    def next_mode(self):
        next_mode = self.settings.vi['next_mode'] or MODE_NORMAL
        return next_mode

    @next_mode.setter
    def next_mode(self, value):
        self.settings.vi['next_mode'] = value

    @property
    def next_mode_command(self):
        next_mode_command = self.settings.vi['next_mode_command']
        return next_mode_command

    @next_mode_command.setter
    def next_mode_command(self, value):
        self.settings.vi['next_mode_command'] = value

    @property
    def repeat_command(self):
        # This property is volatile. It won't be persisted between sessions.
        return VintageState._latest_repeat_command

    @repeat_command.setter
    def repeat_command(self, value):
        VintageState._latest_repeat_command = value

    def parse_motion(self):
        vi_cmd_data = CmdData(self)

        # This should happen only at initialization.
        # XXX: This is effectively zeroing xpos. Shouldn't we move this into new_vi_cmd_data()?
        if vi_cmd_data['xpos'] is None:
            xpos = 0
            if self.view.sel():
                xpos = self.view.rowcol(self.view.sel()[0].b)
            self.xpos = xpos
            vi_cmd_data['xpos'] = xpos

        # Make sure we run NORMAL mode actions taking motions in _MODE_INTERNAL_NORMAL mode.
        if ((self.mode in (MODE_VISUAL, MODE_VISUAL_LINE)) or
            (self.motion and self.action) or
            (self.action and self.mode == MODE_NORMAL)):
                if self.mode not in (MODE_VISUAL, MODE_VISUAL_LINE):
                    vi_cmd_data['mode'] = _MODE_INTERNAL_NORMAL
                else:
                    vi_cmd_data['mode'] = self.mode

        motion = self.motion
        motion_func = None
        if motion:
            motion_func = getattr(motions, self.motion)
        if motion_func:
            vi_cmd_data = motion_func(vi_cmd_data)

        return vi_cmd_data

    def parse_action(self, vi_cmd_data):
        action_func = getattr(actions, self.action)
        if action_func:
            vi_cmd_data = action_func(vi_cmd_data)

        # Notify global state to go ahead with the command if there are selections and the action
        # is ready to be run (which is almost always the case except for some digraphs).
        # NOTE: By virtue of checking for non-empty selections instead of an explicit mode,
        # the user can run actions on selections created outside of Vintageous.
        # This seems to work well.
        if (self.view.has_non_empty_selection_region() and
            # XXX: This check is pretty useless, because we abort early in .run() anyway.
            # Logically, it makes sense, however.
            not vi_cmd_data['is_digraph_start']):
                vi_cmd_data['motion_required'] = False

        return vi_cmd_data

    def eval(self):
        """Examines the current state and decides whether to actually run the action/motion.
        """

        if self.cancel_action:
            # TODO: add a .parse() method that includes boths steps?
            vi_cmd_data = self.parse_motion()
            vi_cmd_data = self.parse_action(vi_cmd_data)
            if vi_cmd_data['must_blink_on_error']:
                utils.blink()
            # Modify the data that determines the mode we'll end up in when the command finishes.
            self.next_mode = vi_cmd_data['_exit_mode']
            # Since we are exiting early, ensure we leave the selections as the commands wants them.
            if vi_cmd_data['_exit_mode_command']:
                self.view.run_command(vi_cmd_data['_exit_mode_command'])
            self.reset()
            return

        # Action + motion, like in "3dj".
        if self.action and self.motion:
            vi_cmd_data = self.parse_motion()
            vi_cmd_data = self.parse_action(vi_cmd_data)

            if not vi_cmd_data['is_digraph_start']:
                # We are about to run an action, so let Sublime Text know we want all editing
                # steps folded into a single sequence. "All editing steps" means slightly different
                # things depending on the mode we are in.
                if vi_cmd_data['_mark_groups_for_gluing']:
                    self.view.run_command('maybe_mark_undo_groups_for_gluing')
                self.view.run_command('vi_run', vi_cmd_data)
                self.reset()
            else:
                # If we have a digraph start, the global data is in an invalid state because we
                # are still missing the complete digraph. Abort and clean up.
                if vi_cmd_data['_exit_mode'] == MODE_INSERT:
                    # We've been requested to change to this mode. For example, we're looking at
                    # CTRL+r,j in INSERTMODE, which is an invalid sequence.
                    # !!! This could be simplified using parameters in .reset(), but then it
                    # wouldn't be obvious what was going on. Don't refactor. !!!
                    utils.blink()
                    self.reset()
                    self.enter_insert_mode()
                elif self.mode != MODE_NORMAL:
                    # Normally we'd go back to normal mode.
                    self.enter_normal_mode()
                    self.reset()

        # Motion only, like in '3j'.
        elif self.motion:
            vi_cmd_data = self.parse_motion()
            self.view.run_command('vi_run', vi_cmd_data)
            self.reset()

        # Action only, like in "d" or "esc". Some actions can be executed without a motion.
        elif self.action:
            vi_cmd_data = self.parse_motion()
            vi_cmd_data = self.parse_action(vi_cmd_data)

            if vi_cmd_data['is_digraph_start']:
                if vi_cmd_data['_change_mode_to']:
                    # XXX: When does this happen? Why are we only interested in MODE_NORMAL?
                    # XXX In response to the above, this must be due to Ctrl+r.
                    if vi_cmd_data['_change_mode_to'] == MODE_NORMAL:
                        self.enter_normal_mode()
                # We know we are not ready.
                return

            # In cases like gg, we might receive the motion here, so check for that.
            # XXX: The above doesn't seem to be true. When is this path reached?
            if self.motion and not self.action:
                self.view.run_command('vi_run', self.parse_motion())
                self.update_status()
                self.reset()
                return

            if not vi_cmd_data['motion_required']:
                # We are about to run an action, so let Sublime Text know we want all editing
                # steps folded into a single sequence. "All editing steps" means slightly different
                # things depending on the mode we are in.
                if vi_cmd_data['_mark_groups_for_gluing']:
                    self.view.run_command('maybe_mark_undo_groups_for_gluing')
                self.view.run_command('vi_run', vi_cmd_data)
                self.reset()

        self.update_status()

    def reset(self, next_mode=None):
        # Some global data must be kept untouched. For example, that's the case of the lastest repeat
        # command. When switching files, Vintageous will be init'ed, and that data will be overwritten,
        # but since we're not creating a new command, it doesn't make sense.
        # FIXME: Not every action should update the latest repeat command.
        if self.action:
            self.update_repeat_command()

        self.motion = None
        self.action = None

        self.register = None
        self.user_input = None
        self.expecting_register = False
        self.expecting_user_input = False
        self.cancel_action = False

        # In MODE_NORMAL_INSERT, we temporarily exit NORMAL mode, but when we get back to
        # it, we need to know the repeat digits, so keep them. An example command for this case
        # would be 5ifoobar\n<esc> starting in NORMAL mode.
        if self.mode == MODE_NORMAL_INSERT:
            return

        self.motion_digits = []
        self.action_digits = []

        if self.next_mode == MODE_INSERT:
            # XXX: Is this redundant?
            self.enter_insert_mode()
            if self.next_mode_command:
                self.view.run_command(self.next_mode_command)
        elif self.next_mode == MODE_NORMAL:
            if self.next_mode_command:
                self.view.run_command(self.next_mode_command)
        else:
            pass

        self.next_mode = MODE_NORMAL

        self.next_mode_command = None

    def update_repeat_command(self):
        """Vintageous manages the repeat command on its own. Vim stores away the latest modifying
           command as the repeat command, and does not wipe it when undoing. On the contrary,
           Sublime Text will update the substitute command as you undo past the current one. The
           then previous latest modifying command becomes the new repeat command, and so on.
        """
        cmd, args, times = self.view.command_history(0, True)
        if not cmd:
            return

        if cmd == 'vi_run' and args['action']:
            try:
                old_cmd, old_args, _ = self.repeat_command
                if (cmd, args) == (old_cmd, old_args):
                    return
            except TypeError:
                pass

            self.repeat_command = cmd, args, times

        elif cmd == 'sequence':
            try:
                old_cmd, old_args, _ = self.repeat_command
            except TypeError:
                return

            if old_cmd == 'sequence':
                pairs = zip(old_args['commands'], args['commands'])
                # Compare pairs of (cmd_name, args_dict).
                update = [(old, new) for (old, new) in pairs if old != new]
                if not update:
                    return

            self.repeat_command = cmd, args, times

        elif cmd != 'vi_run':
            try:
                old_cmd, old_args, _ = self.repeat_command
                if (cmd, args,) == (old_cmd, old_args):
                    return
            except TypeError:
                return

            self.repeat_command = cmd, args, times

    def update_xpos(self):
        state = VintageState(self.view)

        first_sel = self.view.sel()[0]
        xpos = 0
        if state.mode == MODE_VISUAL:
            if first_sel.a < first_sel.b:
                xpos = self.view.rowcol(first_sel.b - 1)[1]
            elif first_sel.a > first_sel.b:
                xpos = self.view.rowcol(first_sel.b)[1]

        elif state.mode == MODE_NORMAL:
            xpos = self.view.rowcol(first_sel.b)[1]

        state.xpos = xpos

    def update_status(self):
        mode_name = mode_to_str(self.mode) or ""
        mode_name = "-- %s --" % mode_name if mode_name else ""
        sublime.status_message(mode_name)


class VintageStateTracker(sublime_plugin.EventListener):
    def on_load(self, view):
        _init_vintageous(view)

    def on_post_save(self, view):
        # Make sure that the carets are within valid bounds. This is for example a concern when
        # `trim_trailing_white_space_on_save` is set to true.
        state = VintageState(view)
        view.run_command('_vi_adjust_carets', {'mode': state.mode})

    def on_query_context(self, view, key, operator, operand, match_all):
        vintage_state = VintageState(view)
        return vintage_state.context.check(key, operator, operand, match_all)


class ViFocusRestorerEvent(sublime_plugin.EventListener):
    def __init__(self):
        self.timer = None

    def action(self):
        self.timer = None

    def on_activated(self, view):
        if self.timer:
            # Switching to a different view; enter normal mode.
            self.timer.cancel()
            _init_vintageous(view)
        else:
            # Switching back from another application. Ignore.
            pass

    def on_deactivated(self, view):
        self.timer = threading.Timer(0.25, self.action)
        self.timer.start()


class IrreversibleTextCommand(sublime_plugin.TextCommand):
    """ Abstract class.

        The undo stack will ignore commands derived from this class. This is
        useful to prevent global state management commands from shadowing
        commands performing edits to the buffer, which are the important ones
        to keep in the undo history.
    """
    def __init__(self, view):
        sublime_plugin.TextCommand.__init__(self, view)

    def run_(self, edit_token, kwargs):
        if kwargs and 'event' in kwargs:
            del kwargs['event']

        if kwargs:
            self.run(**kwargs)
        else:
            self.run()

    def run(self, **kwargs):
        pass
