#!/usr/bin/env python3
"""Bind a host session to an exact shared task or submit an explicit structured command.

Examples: work.py bind --session SESSION --action TASK_UUID
          work.py unbind --session SESSION
          work.py report --action TASK_UUID < command.json
All access uses this host's existing connection. Never supplies another account's key.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from team0_agent_runtime import RuntimeConfig, Team0AgentRuntime, ApiError
from team0_hook import _host_id, _configure_host_environment, _load_saved_credential


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['bind', 'unbind', 'read', 'report', 'manage'])
    parser.add_argument('--session')
    parser.add_argument('--action')
    args = parser.parse_args()
    host = _host_id()
    _configure_host_environment(host)
    _load_saved_credential(host)
    runtime = Team0AgentRuntime(RuntimeConfig.from_environ())
    try:
        if args.operation in {'bind', 'unbind'}:
            if not args.session or (args.operation == 'bind' and not args.action):
                parser.error('Binding requires --session and --action; unbind requires --session')
            runtime.bind_work(session_id=args.session, action_id=args.action if args.operation == 'bind' else None)
            result = {'bound': args.operation == 'bind'}
        elif args.operation == 'read':
            if not runtime.client:
                raise ApiError('work.not_configured', status=403)
            result = runtime.client.work_tool('work_read', {'action_id': args.action or ''}, request_id='work-read')
        else:
            result = runtime.work_command(action_id=args.action, command=json.load(sys.stdin), manage=args.operation == 'manage')
        print(json.dumps(result, ensure_ascii=False, default=str))
    except ApiError as error:
        print(json.dumps({'error': error.code, 'status': error.status}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
