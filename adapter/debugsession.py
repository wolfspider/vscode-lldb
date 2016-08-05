import sys
import logging
import os.path
import shlex
import traceback
import lldb
from . import debugevents
from . import handles
from . import terminal
from . import PY2

log = logging.getLogger('debugsession')

class DebugSession:

    def __init__(self, event_loop, send_message):
        DebugSession.current = self
        self.event_loop = event_loop
        self.send_message = send_message
        self.var_refs = handles.Handles()
        self.ignore_bp_events = False
        self.breakpoints = dict() # { file : { line : SBBreakpoint } }
        self.fn_breakpoints = dict() # { name : SBBreakpoint }
        self.exc_breakpoints = []
        self.target = None
        self.process = None
        self.terminal = None
        self.launch_args = None

    def initialize_request(self, args):
        self.line_offset = 0 if args.get('linesStartAt1', True) else 1
        self.col_offset = 0 if args.get('columnsStartAt1', True) else 1
        self.debugger = lldb.SBDebugger.Create()
        log.info('LLDB version: %s', self.debugger.GetVersionString())
        self.debugger.SetAsync(True)
        self.event_listener = lldb.SBListener('DebugSession')
        listener_handler = debugevents.AsyncListener(self.event_listener,
                self.event_loop.make_dispatcher(self.handle_debugger_event))
        self.listener_handler_token = listener_handler.start()
        return { 'supportsConfigurationDoneRequest': True,
                 'supportsEvaluateForHovers': True,
                 'supportsFunctionBreakpoints': True,
                 'supportsConditionalBreakpoints': True,
                 'supportsSetVariable': True }

    def launch_request(self, args):
        self.exec_commands(args.get('initCommands'))
        self.target = self.create_target(args)
        self.send_event('initialized', {})
        # defer actual launching till configurationDone request, so that
        # we can receive and set initial breakpoints before the target starts running
        self.do_launch = self.launch
        self.launch_args = args
        return AsyncResponse

    def launch(self, args):
        self.exec_commands(args.get('preRunCommands'))
        flags = 0
        # argumetns
        target_args = args.get('args', None)
        if target_args is not None:
            if isinstance(target_args, string_type):
                target_args = shlex.split(target_args)
            target_args = [str(arg) for arg in target_args]
        # environment
        env = args.get('env', None)
        envp = [str('%s=%s' % pair) for pair in os.environ.items()]
        if env is not None: # Convert dict to a list of 'key=value' strings
            envp = envp + ([str('%s=%s' % pair) for pair in env.items()])
        # stdio
        stdio = self.get_stdio_config(args)
        # open a new terminal window if needed
        if '*' in stdio:
            if 'linux' in sys.platform:
                self.terminal = terminal.create()
                stdio = [self.terminal.tty if s == '*' else s for s in stdio]
            else:
                # OSX LLDB supports this natively.
                # On Windows LLDB always creates a new console window (even if stdio is redirected).
                flags |= lldb.eLaunchFlagLaunchInTTY | lldb.eLaunchFlagCloseTTYOnExit
                stdio = [None if s == '*' else s for s in stdio]
        # working directory
        work_dir = opt_str(args.get('cwd', None))
        stop_on_entry = args.get('stopOnEntry', False)
        # launch!
        error = lldb.SBError()
        self.process = self.target.Launch(self.event_listener,
            target_args, envp, stdio[0], stdio[1], stdio[2],
            work_dir, flags, stop_on_entry, error)
        if not error.Success():
            self.console_msg(error.GetCString())
            self.send_event('terminated', {})
            raise UserError('Process launch failed.')

        assert self.process.IsValid()
        self.process_launched = True

    def get_stdio_config(self, args):
        stdio = args.get('stdio', None)
        missing = () # None is a valid value here, so we need a new one to designate 'missing'
        if isinstance(stdio, dict): # Flatten it into a list
            stdio = [stdio.get('stdin', missing),
                     stdio.get('stdout', missing),
                     stdio.get('stderr', missing)]
        elif stdio is None or isinstance(stdio, string_type):
            stdio = [stdio] * 3
        elif isinstance(stdio, list):
            stdio.extend([missing] * (3-len(stdio))) # pad up to 3 items
        else:
            raise UserError('stdio must be either a string, a list or an object')
        # replace all missing's with the previous stream's value
        for i in range(0, len(stdio)):
            if stdio[i] == missing:
                stdio[i] = stdio[i-1] if i > 0 else None
        stdio = list(map(opt_str, stdio))
        return stdio

    def attach_request(self, args):
        self.exec_commands(args.get('initCommands'))
        self.target = self.create_target(args)
        self.send_event('initialized', {})
        self.do_launch = self.attach
        self.launch_args = args
        return AsyncResponse

    def attach(self, args):
        self.exec_commands(args.get('preRunCommands'))

        error = lldb.SBError()
        if args.get('pid', None) is not None:
            self.process = self.target.AttachToProcessWithID(self.event_listener, args['pid'], error)
        else:
            self.process = self.target.AttachToProcessWithName(self.event_listener, str(args['program']), False, error)

        if not error.Success():
            self.console_msg(error.GetCString())
            raise UserError('Failed to attach to process.')

        assert self.process.IsValid()
        self.process_launched = False

    def create_target(self, args):
        program = args['program']
        load_dependents = not args.get('noDebug', False)
        error = lldb.SBError()
        target = self.debugger.CreateTarget(str(program), lldb.LLDB_ARCH_DEFAULT, None, load_dependents, error)
        if not error.Success() and 'win32' in sys.platform:
            # On Windows, try appending '.exe' extension, to make launch configs more uniform.
            program += '.exe'
            error2 = lldb.SBError()
            target = self.debugger.CreateTarget(str(program), lldb.LLDB_ARCH_DEFAULT, None, load_dependents, error2)
            if error2.Success():
                args['program'] = program
        if not error.Success():
            raise UserError('Could not initialize debug target: ' + error.GetCString())
        target.GetBroadcaster().AddListener(self.event_listener, lldb.SBTarget.eBroadcastBitBreakpointChanged)
        return target

    def exec_commands(self, commands):
        if commands is not None:
            interp = self.debugger.GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            for command in commands:
                interp.HandleCommand(str(command), result)
                output = result.GetOutput() if result.Succeeded() else result.GetError()
                self.console_msg(output)

    def setBreakpoints_request(self, args):
        result = []
        if not self.launch_args.get('noDebug', False):
            self.ignore_bp_events = True
            source = args['source']
            file = str(source['path'])
            req_bps = args['breakpoints']
            req_bp_lines = [req['line'] for req in req_bps]
            # Existing breakpints indexed by line
            curr_bps = self.breakpoints.setdefault(file, {})
            # Existing breakpints that were removed
            for line, bp in list(curr_bps.items()):
                if line not in req_bp_lines:
                    self.target.BreakpointDelete(bp.GetID())
                    del curr_bps[line]
            # Added or updated
            for req in req_bps:
                line = req['line']
                bp = curr_bps.get(line, None)
                if bp is None:
                    bp = self.target.BreakpointCreateByLocation(file, line)
                    curr_bps[line] = bp
                cond = opt_str(req.get('condition', None))
                if cond != bp.GetCondition():
                    bp.SetCondition(cond)
                result.append(self.make_bp_resp(bp))
            self.ignore_bp_events = False

        return { 'breakpoints': result }

    def setFunctionBreakpoints_request(self, args):
        result = []
        if not self.launch_args.get('noDebug', False):
            self.ignore_bp_events = True
            # Breakpoint requests indexed by function name
            req_bps = args['breakpoints']
            req_bp_names = [req['name'] for req in req_bps]
            # Existing breakpints that were removed
            for name,bp in list(self.fn_breakpoints.items()):
                if name not in req_bp_names:
                    self.target.BreakpointDelete(bp.GetID())
                    del self.fn_breakpoints[name]
            # Added or updated
            result = []
            for req in req_bps:
                name = req['name']
                bp = self.fn_breakpoints.get(name, None)
                if bp is None:
                    bp = self.target.BreakpointCreateByRegex(str(name))
                    self.fn_breakpoints[name] = bp
                cond = opt_str(req.get('condition', None))
                if cond != bp.GetCondition():
                    bp.SetCondition(cond)
                result.append(self.make_bp_resp(bp))
            self.ignore_bp_events = False

        return { 'breakpoints': result }

    # Create breakpoint location info for a response message
    def make_bp_resp(self, bp):
        if bp.num_locations == 0:
            return { 'id': bp.GetID(), 'verified': False }
        le = bp.GetLocationAtIndex(0).GetAddress().GetLineEntry()
        fs = le.GetFileSpec()
        if not (le.IsValid() and fs.IsValid()):
            return { 'id': bp.GetID(), 'verified': True }
        source = { 'name': fs.basename, 'path': fs.fullpath }
        return { 'id': bp.GetID(), 'verified': True, 'source': source, 'line': le.line }

    def setExceptionBreakpoints_request(self, args):
        if not self.launch_args.get('noDebug', False):
            filters = args['filters']
            for bp in self.exc_breakpoints:
                self.target.BreakpointDelete(bp.GetID())
            self.exc_breakpoints = []

            source_languages = self.launch_args.get('sourceLanguages', [])
            set_all = 'all' in filters
            set_uncaught = 'uncaught' in filters
            for lang in source_languages:
                bp_setters = DebugSession.lang_exc_bps.get(lang)
                if bp_setters is not None:
                    if set_all:
                        bp = bp_setters[0](self.target)
                        self.exc_breakpoints.append(bp)
                    if set_uncaught:
                        bp = bp_setters[1](self.target)
                        self.exc_breakpoints.append(bp)

    lang_exc_bps = {
        'rust': (lambda target: target.BreakpointCreateByName('rust_panic'),
                 lambda target: target.BreakpointCreateByName('abort')),
        'cpp': (lambda target: target.BreakpointCreateForException(lldb.eLanguageTypeC_plus_plus, False, True),
                lambda target: target.BreakpointCreateByName('terminate')),
    }

    def configurationDone_request(self, args):
        try:
            result = self.do_launch(self.launch_args)
            # On Linux, LLDB doesn't seem to automatically generate a stop event for stop_on_entry
            if self.process.GetState() == lldb.eStateStopped:
                self.notify_target_stopped(None)
        except Exception as e:
            result = e
        # do_launch is asynchronous so we need to send its result
        self.send_response(self.launch_args['response'], result)

    def pause_request(self, args):
        self.process.Stop()

    def continue_request(self, args):
        # variable handles will be invalid after running,
        # so we may as well clean them up now
        self.var_refs.reset()
        self.process.Continue()

    def next_request(self, args):
        self.var_refs.reset()
        tid = args['threadId']
        self.process.GetThreadByID(tid).StepOver()

    def stepIn_request(self, args):
        self.var_refs.reset()
        tid = args['threadId']
        self.process.GetThreadByID(tid).StepInto()

    def stepOut_request(self, args):
        self.var_refs.reset()
        tid = args['threadId']
        self.process.GetThreadByID(tid).StepOut()

    def threads_request(self, args):
        threads = []
        for thread in self.process:
            tid = thread.GetThreadID()
            threads.append({ 'id': tid, 'name': '%s:%d' % (thread.GetName(), tid) })
        return { 'threads': threads }

    def stackTrace_request(self, args):
        thread = self.process.GetThreadByID(args['threadId'])
        start_frame = args.get('startFrame', 0)
        levels = args.get('levels', sys.maxsize)
        if start_frame + levels > thread.num_frames:
            levels = thread.num_frames - start_frame
        stack_frames = []
        for i in range(start_frame, start_frame + levels):
            frame = thread.frames[i]
            stack_frame = { 'id': self.var_refs.create(frame) }
            fn_name = frame.GetFunctionName()
            if fn_name is None:
                fn_name = str(frame.GetPCAddress())
            stack_frame['name'] = fn_name

            le = frame.GetLineEntry()
            if le.IsValid():
                fs = le.GetFileSpec()
                # VSCode gets confused if the path contains funky stuff like a double-slash
                full_path = os.path.normpath(fs.fullpath)
                stack_frame['source'] = { 'name': fs.basename, 'path': full_path }
                stack_frame['line'] = le.GetLine()
                stack_frame['column'] = le.GetColumn()
            stack_frames.append(stack_frame)
        return { 'stackFrames': stack_frames, 'totalFrames': len(thread) }

    def scopes_request(self, args):
        locals = { 'name': 'Locals', 'variablesReference': args['frameId'], 'expensive': False }
        return { 'scopes': [locals] }

    def variables_request(self, args):
        variables = []
        obj = self.var_refs.get(args['variablesReference'])
        if obj is None:
            raise Exception('Invalid variable reference')

        if type(obj) is lldb.SBFrame:
            # args, locals, statics, in_scope_only
            vars = obj.GetVariables(True, True, False, True)
        elif type(obj) is lldb.SBValue:
            vars = obj
        else: # ('synthetic', var)
            vars = obj[1].GetNonSyntheticValue()

        for var in vars:
            name, value, dtype, ref = self.parse_var(var)
            # Sometimes LLDB returns junk entries with empty names and values
            if name is not None:
                if value is None: value = dtype
                variable = { 'name': name, 'value': value, 'type': dtype, 'variablesReference': ref }
                variables.append(variable)

        if type(vars) is lldb.SBValue and vars.IsSynthetic():
            ref = self.var_refs.create(('synthetic', vars))
            variable = { 'name': '[raw]', 'value': vars.GetTypeName(), 'variablesReference': ref }
            variables.append(variable)

        return { 'variables': variables }

    def evaluate_request(self, args):
        context = args['context']
        expr = str(args['expression'])
        if context in ['watch', 'hover']:
            return self.evaluate_expr(args, expr)
        elif expr.startswith('?'): # "?<expr>" in 'repl' context
            return self.evaluate_expr(args, expr[1:])
        # Else evaluate as debugger command

        # set up evaluation context
        frame = self.var_refs.get(args.get('frameId', None), None)
        if frame is not None:
            thread = frame.GetThread()
            self.process.SetSelectedThread(thread)
            thread.SetSelectedFrame(frame.GetFrameID())
        # evaluate
        interp = self.debugger.GetCommandInterpreter()
        result = lldb.SBCommandReturnObject()
        interp.HandleCommand(str(expr), result)
        output = result.GetOutput() if result.Succeeded() else result.GetError()
        # returning output as result would display all line breaks as '\n'
        self.console_msg(output)
        return { 'result': '' }

    def evaluate_expr(self, args, expr):
        frame = self.var_refs.get(args.get('frameId', 0), None)
        if frame is None:
            return
        var = frame.EvaluateExpression(expr)
        error = var.GetError()
        if error.Success():
            _, value, dtype, ref = self.parse_var(var)
            if value is None: value = dtype
            return { 'result': value, 'variablesReference': ref }
        else:
            message = error.GetCString()
            if args['context'] == 'repl':
                self.console_msg(message)
            else:
                raise UserError(message.replace('\n', '; '), no_console=True)

    def parse_var(self, var):
        name = var.GetName()
        value = self.get_var_value(var)
        dtype = var.GetTypeName()
        ref = self.var_refs.create(var) if var.GetNumChildren() > 0 else 0
        return name, value, dtype, ref

    def get_var_value(self, var):
        value = var.GetValue()
        if value is None:
            value = var.GetSummary()
            if value is not None:
                value = value.replace('\n', '') # VSCode won't display line breaks
        if PY2 and value is not None:
            value = value.decode('latin1') # or else json will try to treat it as utf8
        return value

    def setVariable_request(self, args):
        obj = self.var_refs.get(args['variablesReference'])
        if obj is None:
            raise Exception('Invalid variable reference')

        name = str(args['name'])
        if type(obj) is lldb.SBFrame:
            # args, locals, statics, in_scope_only
            var = obj.FindVariable(name)
        elif type(obj) is lldb.SBValue:
            var = obj.GetChildMemberWithName(name)
            if not var.IsValid():
                var = obj.GetValueForExpressionPath(name)
        else: # ('synthetic', var)
            var = obj[1]

        if not var.IsValid():
            raise Exception('Could not get a child with name ' + name)

        error = lldb.SBError()
        if not var.SetValueFromCString(str(args['value']), error):
            self.console_msg(error.GetCString())
            raise UserError(error.GetCString())
        return { 'value': self.get_var_value(var) }

    def disconnect_request(self, args):
        if self.process:
            if self.process_launched:
                self.process.Kill()
            else:
                self.process.Detach()
        self.process = None
        self.target = None
        self.terminal = None
        self.event_loop.stop()

    # handles messages from VSCode
    def handle_message(self, request):
        if request is None:
            # Client connection lost; treat this the same as a normal disconnect.
            self.disconnect_request(None)
            return

        command =  request['command']
        args = request.get('arguments', {})
        log.debug('### Handling command: %s', command)

        response = { 'type': 'response', 'command': command,
                     'request_seq': request['seq'], 'success': False }
        args['response'] = response

        handler = getattr(self, command + '_request', None)
        if handler is not None:
            try:
                result = handler(args)
                # `result` being an AsyncResponse means that the handler is asynchronous and
                # will respond at a later time.
                if result is AsyncResponse: return
            except Exception as e:
                result = e
            self.send_response(response, result)
        else:
            log.warning('No handler for %s', command)
            response['success'] = False
            self.send_message(response)

    # sends response with `result` as a body
    def send_response(self, response, result):
        if result is None or isinstance(result, dict):
            response['success'] = True
            response['body'] = result
        elif isinstance(result, UserError):
            if not result.no_console:
                self.console_msg('Error: ' + str(result))
            response['success'] = False
            response['body'] = { 'error': { 'id': 0, 'format': str(result), 'showUser': True } }
        elif isinstance(result, Exception):
            tb = traceback.format_exc(result)
            log.error('Internal error:\n' + tb)
            msg = 'Internal error: ' + str(result)
            self.console_msg(msg)
            response['success'] = False
            response['body'] = { 'error': { 'id': 0, 'format': msg, 'showUser': True } }
        else:
            assert False, "Invalid result type: %s" % result
        self.send_message(response)

    # handles debugger notifications
    def handle_debugger_event(self, event):
        if lldb.SBProcess.EventIsProcessEvent(event):
            ev_type = event.GetType()
            if ev_type == lldb.SBProcess.eBroadcastBitStateChanged:
                state = lldb.SBProcess.GetStateFromEvent(event)
                if state == lldb.eStateStopped:
                    if not lldb.SBProcess.GetRestartedFromEvent(event):
                        self.notify_target_stopped(event)
                elif state == lldb.eStateExited:
                    exit_code = self.process.GetExitStatus()
                    self.console_msg('Process exited with code %d' % exit_code)
                    self.send_event('exited', { 'exitCode': exit_code })
                    self.send_event('terminated', {}) # TODO: VSCode doesn't seem to handle 'exited' for now
                elif state in [lldb.eStateCrashed, lldb.eStateDetached]:
                    self.send_event('terminated', {})
            elif ev_type & (lldb.SBProcess.eBroadcastBitSTDOUT | lldb.SBProcess.eBroadcastBitSTDERR) != 0:
                self.notify_stdio(ev_type)
        elif lldb.SBBreakpoint.EventIsBreakpointEvent(event) and not self.ignore_bp_events:
            self.notify_breakpoint(event)

    def notify_target_stopped(self, lldb_event):
        event = { 'allThreadsStopped': True } # LLDB always stops all threads
        # Find the thread that has caused this stop
        for thread in self.process:
            stop_reason = thread.GetStopReason()
            if stop_reason == lldb.eStopReasonBreakpoint:
                event['threadId'] = thread.GetThreadID()
                bp_id = thread.GetStopReasonDataAtIndex(0)
                for bp in self.exc_breakpoints:
                    if bp.GetID() == bp_id:
                        event['reason'] = 'exception'
                        break;
                else:
                    event['reason'] = 'breakpoint'
                break
            elif stop_reason == lldb.eStopReasonException:
                event['threadId'] = thread.GetThreadID()
                event['reason'] = 'exception'
                break
            elif stop_reason in [lldb.eStopReasonTrace, lldb.eStopReasonPlanComplete]:
                event['threadId'] = thread.GetThreadID()
                event['reason'] = 'step'
                break
            elif stop_reason == lldb.eStopReasonSignal:
                event['threadId'] = thread.GetThreadID()
                event['reason'] = 'signal'
                event['text'] = thread.GetStopReasonDataAtIndex(0)
                break
        else:
            event['reason'] = 'unknown'
        self.send_event('stopped', event)

    def notify_stdio(self, ev_type):
        if ev_type == lldb.SBProcess.eBroadcastBitSTDOUT:
            read_stream = self.process.GetSTDOUT
            category = 'stdout'
        else:
            read_stream = self.process.GetSTDERR
            category = 'stderr'
        output = read_stream(1024)
        while output:
            self.send_event('output', { 'category': category, 'output': output })
            output = read_stream(1024)

    def notify_breakpoint(self, event):
        bp = lldb.SBBreakpoint.GetBreakpointFromEvent(event)
        bp_info = self.make_bp_resp(bp)
        self.send_event('breakpoint', { 'reason': 'new', 'breakpoint': bp_info })

    def send_event(self, event, body):
        message = {
            'type': 'event',
            'seq': 0,
            'event': event,
            'body': body
        }
        self.send_message(message)

    # Write a message to debug console
    def console_msg(self, output):
        self.send_event('output', { 'category': 'console', 'output': output })

# For when we need to let user know they screwed up
class UserError(Exception):
    def __init__(self, message, no_console=False):
        Exception.__init__(self, message)
        # Don't copy error message to debug console if this is set
        self.no_console = no_console

# Result type for async handlers
class AsyncResponse:
    pass

def opt_str(s):
    return str(s) if s != None else None

string_type = basestring if PY2 else str
