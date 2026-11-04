"""The ``telemetry`` command-line interface.

A complete, independent front end over ``OperatorService``: every user
operation in ``toyota_telemetry.operator.OPERATIONS`` has a command here. The
module parses arguments and formats results; it makes no decisions of its own.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal as signals_module
import sys
import threading
from typing import Any, Callable

from . import __version__
from .collector import CollectionCrashed, parse_public_event
from .operator.editing import (
    REQUIREMENT_LABELS,
    format_condition,
    format_duration,
    parse_condition,
    parse_duration,
)
from .operator.live import ConfirmationRefused, LiveBusy, record_preview, require_previewed
from .operator.service import OperatorService
from .operator.sources import NormalizedFileSource, TisDatabaseSource
from .operator.verification import RecipeRequired
from .project import find_project_root


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3


class Refused(RuntimeError):
    """The operator declined, or confirmation rules refused, a live start."""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "init":
            return _init(args)
        service = OperatorService(args.project_root.resolve() if args.project_root else find_project_root())
        return args.handler(service, args)
    except (Refused, ConfirmationRefused, LiveBusy) as exc:
        return _error(exc, args, EXIT_REFUSED)
    except (CollectionCrashed, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        return _error(exc, args, EXIT_FAILED)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", type=Path, help="workspace folder (default: search upward)")
    common.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("selectors", nargs="*", metavar="SELECTOR",
                          help="ENDPOINT, ENDPOINT/0xNNNN, ENDPOINT/0xNNNN/FIRST..LAST, or ECU/0xNNNN/FIRST..LAST")
    selector.add_argument("--ecu", type=int, action="append", default=[], help="only this ECU key")
    selector.add_argument("--state", action="append", default=[], dest="states",
                          help="verified, not-verified, failed, targeted, or untargeted")
    selector.add_argument("--search", help="signal name or reference contains TEXT")
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true", help="show the effect without changing anything")

    parser = argparse.ArgumentParser(
        prog="telemetry",
        description="Verify, target, collect, and analyze diagnostic telemetry in one project workspace.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, operations, handler: Callable, help_text: str, parents=(), parent=commands):
        sub = parent.add_parser(name, help=help_text, description=help_text, parents=[common, *parents])
        operations = (operations,) if isinstance(operations, str) else tuple(operations)
        sub.set_defaults(handler=handler, operations=operations)
        return sub

    init = command("init", "init", _init, "create an empty project workspace")
    init.add_argument("folder", type=Path, help="new or empty folder, outside any repository")
    init.add_argument("--project-id", help="stable project id (default: from the folder name)")
    init.add_argument("--profile-id", help="vehicle profile id (default: the project id)")
    command("status", "status", _status, "show readiness and the next action")
    command("check", "check", _check, "validate project files and the candidate list")

    connector = commands.add_parser("connector", help="machine-local CAN connector settings")
    connector_commands = connector.add_subparsers(dest="connector_command", required=True)
    command("show", "connector.show", _connector_show, "show connector settings", parent=connector_commands)
    connector_set = command(
        "set", "connector.set", _connector_set, "save connector settings", [dry], parent=connector_commands
    )
    connector_set.add_argument("kind", choices=["pcan", "j2534", "gsusb", "slcan"])
    connector_set.add_argument("--channel", help="PCAN channel, e.g. PCAN_USBBUS1")
    connector_set.add_argument("--dll", help="J2534 driver DLL path")
    connector_set.add_argument("--vendor-id", help="gs_usb vendor ID (4 hex digits)")
    connector_set.add_argument("--product-id", help="gs_usb product ID (4 hex digits)")
    connector_set.add_argument("--serial", help="gs_usb serial number")
    connector_set.add_argument("--port", help="slcan serial port")
    connector_set.add_argument("--executable", help="worker executable (default: the bundled worker)")
    connector_check = command(
        "check", "connector.check", _connector_check,
        "validate settings and run the worker self-test without opening the device",
        parent=connector_commands,
    )
    connector_check.add_argument("--no-self-test", action="store_true")

    source = commands.add_parser("source", help="candidate signal sources")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_import = source_commands.add_parser("import", help="preview or apply a candidate source")
    import_kinds = source_import.add_subparsers(dest="source_kind", required=True)
    tis = command("tis", ("source.preview", "source.apply"), _source_import, "read installed TIS (Techstream) databases",
                  parent=import_kinds)
    tis.add_argument("--database", type=Path, required=True, help="Techstream region folder containing DB/")
    tis.add_argument("--phase", type=int, action="append", default=[], help="database phase (default 5)")
    tis.add_argument("--ecu", type=int, action="append", default=[], help="only this ECU key")
    tis.add_argument("--techstream-bin", type=Path, help="Techstream bin folder")
    tis.add_argument("--code-page", type=int, default=932, help="database text code page")
    tis.add_argument("--apply", action="store_true", help="replace this source's rows after previewing")
    normalized = command("file", ("source.preview", "source.apply"), _source_import, "read a normalized JSON row file",
                         parent=import_kinds)
    normalized.add_argument("path", type=Path)
    normalized.add_argument("--apply", action="store_true", help="replace this source's rows after previewing")

    cache = commands.add_parser("cache", help="derived cache maintenance")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    command("rebuild", "cache.rebuild", _cache_rebuild, "rebuild the disposable SQLite cache",
            parent=cache_commands)

    listing = command("signals", ("signals.list", "signals.show"), _signals, "list endpoints, DIDs, and signals; "
                      "`signals show SELECTOR` shows details", [selector])
    listing.add_argument("--limit", type=int, default=200, help="maximum signal rows to print")

    endpoint = commands.add_parser("endpoint", help="endpoint candidates and confirmation")
    endpoint_commands = endpoint.add_subparsers(dest="endpoint_command", required=True)
    add = command("add", "endpoint.add", _endpoint_add, "add a manual endpoint candidate",
                  parent=endpoint_commands)
    add.add_argument("address", metavar="TESTER:RESPONSE:RECIPE", help="e.g. 0x7E0:0x7E8:extended")
    add.add_argument("--ecu", type=int, action="append", required=True, help="ECU key served here")
    confirm = command("confirm", "endpoint.confirm", _endpoint_confirm,
                      "write a responding endpoint into tracked transport", [dry], parent=endpoint_commands)
    confirm.add_argument("endpoint_id")
    confirm.add_argument("--ecu", type=int, action="append", required=True, help="ECU key served here")
    confirm.add_argument("--source", action="append", required=True, dest="provenance",
                         help="tracked evidence document, e.g. docs/evidence/probe.md")

    verify = command("verify", "verify", _verify,
                     "LIVE: probe selected endpoints, then read selected DIDs", [selector, dry])
    verify.add_argument("--recipe", help="session recipe for endpoints that are not verified yet")
    verify.add_argument("--yes", action="store_true", help="start without a prompt (needs a matching --dry-run)")

    signal_cmd = commands.add_parser("signal", help="signal definition and review")
    signal_commands = signal_cmd.add_subparsers(dest="signal_command", required=True)
    signal_set = command("set", "signal.set", _signal_set, "edit signal definitions", [selector, dry],
                         parent=signal_commands)
    signal_set.add_argument("--name", help="display name ('' clears the override)")
    signal_set.add_argument("--formula", help="decode formula in terms of raw ('' clears)")
    signal_set.add_argument("--unit", help="unit ('' clears)")
    signal_set.add_argument("--evidence", dest="evidence_state", help="evidence state, e.g. 'Observed and replayed'")
    signal_set.add_argument("--note", help="review note")
    signal_set.add_argument("--variant", help="catalog variant id to use ('' clears)")

    did = commands.add_parser("did", help="shared endpoint+DID acquisition settings")
    did_commands = did.add_subparsers(dest="did_command", required=True)
    did_set = command("set", "did.set", _did_set, "edit shared DID settings", [selector, dry],
                      parent=did_commands)
    did_set.add_argument("--interval", type=int, help="target interval in ms")
    did_set.add_argument("--max-interval", type=int, help="maximum interval in ms")
    did_set.add_argument("--priority", type=int, help="0-100")
    inclusion = did_set.add_mutually_exclusive_group()
    inclusion.add_argument("--include", action="store_true")
    inclusion.add_argument("--exclude", action="store_true")

    target = commands.add_parser("target", help="collection targets")
    target_commands = target.add_subparsers(dest="target_command", required=True)
    target_set = command("set", "target.set", _target_set, "set completion requirements; 0 removes one",
                         [selector, dry], parent=target_commands)
    target_set.add_argument("--min-samples", type=int)
    target_set.add_argument("--min-runs", type=int)
    target_set.add_argument("--min-span", help="duration such as 30m or 2h")
    target_set.add_argument("--required-states", help="comma-separated raw states that must be seen")
    target_set.add_argument("--min-transitions", type=int)
    target_set.add_argument("--min-changes", type=int)
    target_set.add_argument("--min-difference", type=float, help="decoded difference that counts as a change")
    target_set.add_argument("--min-applicable", help="duration observed while the condition is true")
    target_set.add_argument("--when", help="condition, e.g. '372/0x1002/0..15 > 10'; 'none' clears")
    target_set.add_argument("--when-max-age", type=int, default=2000, help="condition value age limit in ms")
    target_inclusion = target_set.add_mutually_exclusive_group()
    target_inclusion.add_argument("--include", action="store_true")
    target_inclusion.add_argument("--exclude", action="store_true")
    command("clear", "target.clear", _target_clear, "remove targets (recordings are kept)",
            [selector, dry], parent=target_commands)

    collect = command("collect", ("collect.readiness", "collect"), _collect,
                      "LIVE: collect the readiness-checked scope; --dry-run only checks readiness", [dry])
    collect.add_argument("--endpoint", help="endpoint to collect when targets span several")
    collect.add_argument("--note", help="free-text run note")
    collect.add_argument("--yes", action="store_true", help="start without a prompt (needs a matching --dry-run)")

    actions = ("list", "show", "recover", "trash", "restore", "empty-trash", "export")
    recordings = command("recordings", tuple(f"recordings.{name}" for name in actions), _recordings,
                         "list drives; `recordings show|recover|trash|restore DRIVE`; `empty-trash`; `export`")
    recordings.add_argument("action", nargs="?", choices=list(actions), default="list")
    recordings.add_argument("drive", nargs="?",
                            help="drive start such as 2026-09-01T08:15 (or any run ID in it)")
    recordings.add_argument("--trash", action="store_true", help="list: the drives in the trash")
    recordings.add_argument("--runs", action="store_true", dest="list_runs", help="list: individual runs")
    recordings.add_argument("--yes", action="store_true", help="empty-trash: delete without a prompt")
    recordings.add_argument("--signal", action="append", default=[], dest="signals",
                            help="export: selector or signal reference (repeatable)")
    recordings.add_argument("--drive", action="append", default=[], dest="drives",
                            help="export: only this drive (repeatable)")
    recordings.add_argument("--run", action="append", default=[], dest="runs", help="export: only this run")
    recordings.add_argument("--output", type=Path, help="export: CSV file (default: standard output)")

    export = command("export", "export", _export,
                     "write the reviewed result and curated inputs into a deliverable folder")
    export.add_argument("destination", type=Path, help="deliverable folder outside the workspace")
    return parser


def cli_operations(parser: argparse.ArgumentParser | None = None) -> set[str]:
    """Operation ids reachable from the parser, used by the parity test."""

    found: set[str] = set()

    def visit(node: argparse.ArgumentParser) -> None:
        found.update(node._defaults.get("operations", ()))
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    visit(child)

    visit(parser or build_parser())
    return found


# Handlers ---------------------------------------------------------------


def _status(service: OperatorService, args: argparse.Namespace) -> int:
    status = service.status()
    if args.as_json:
        return _emit(status.as_dict())
    lines = [f"Project   {status.project_root}"]
    if not status.valid:
        lines.append(f"Problem   {status.error}")
    else:
        counts = status.counts
        lines += [
            f"Profile   {status.profile_id}",
            f"Connector {status.connector or 'not configured'}",
            f"Candidates {status.candidate_rows} rows from {status.sources} source(s)",
            f"Endpoints {counts.get('confirmed_endpoints', 0)} confirmed of {counts.get('endpoints', 0)}",
            f"DIDs      {counts.get('verified_dids', 0)} verified, {counts.get('failed_dids', 0)} failed, "
            f"{counts.get('dids', 0)} total",
            f"Targets   {counts.get('targets', 0)} set, {status.targets_complete} complete",
            f"Runs      {status.runs} recorded"
            + (f", {len(status.recoverable_runs)} need recovery" if status.recoverable_runs else ""),
        ]
        if status.live_busy:
            lines.append("LIVE      a live job is running")
        if status.collection is not None:
            lines.append("Collect   " + ("ready" if status.collection["ready"] else "not ready"))
    if status.next_action:
        lines += ["", f"Next: {status.next_action.text}", f"  {status.next_action.command}"]
    print("\n".join(lines))
    return EXIT_OK if status.valid else EXIT_FAILED


def _check(service: OperatorService, args: argparse.Namespace) -> int:
    result = service.check()
    if args.as_json:
        _emit(result.as_dict())
    else:
        for note in result.notes:
            print(f"ok: {note}")
        for problem in result.problems:
            print(f"PROBLEM: {problem}")
    return EXIT_OK if result.ok else EXIT_FAILED


def _connector_show(service: OperatorService, args: argparse.Namespace) -> int:
    document = service.connector()
    if args.as_json:
        return _emit({"connector": document})
    if document is None:
        print("No connector configured. Use: telemetry connector set KIND ...")
        return EXIT_OK
    backend = document["backend"]
    print(f"{backend['kind']}  executable={backend['executable'] or 'bundled'}")
    for name, value in sorted(backend["settings"].items()):
        print(f"  {name} = {value}")
    return EXIT_OK


def _connector_set(service: OperatorService, args: argparse.Namespace) -> int:
    settings = {
        name: value for name, value in (
            ("channel", args.channel), ("dll", args.dll), ("vendor_id", args.vendor_id),
            ("product_id", args.product_id), ("serial", args.serial), ("port", args.port),
        ) if value is not None
    }
    document = service.connector_document(args.kind, settings, args.executable)
    check = service.check_connector(document, run_self_test=False)
    if not args.dry_run:
        service.save_connector(document)
    if args.as_json:
        return _emit({"connector": document, "saved": not args.dry_run, "check": check.as_dict()})
    print(("Would save" if args.dry_run else "Saved") + f" {args.kind} connector settings (machine-local, never exported).")
    for problem in check.problems:
        print(f"warning: {problem}")
    return EXIT_OK


def _connector_check(service: OperatorService, args: argparse.Namespace) -> int:
    check = service.check_connector(run_self_test=not args.no_self_test)
    if args.as_json:
        _emit(check.as_dict())
    else:
        for note in check.notes:
            print(f"ok: {note}")
        for problem in check.problems:
            print(f"PROBLEM: {problem}")
        if check.ok:
            print("Connector settings are valid. The adapter was not opened.")
    return EXIT_OK if check.ok else EXIT_FAILED


def _source_import(service: OperatorService, args: argparse.Namespace) -> int:
    if args.source_kind == "tis":
        source = TisDatabaseSource(
            args.database, phases=args.phase or (5,), ecu_keys=args.ecu,
            techstream_bin_directory=args.techstream_bin, text_code_page=args.code_page,
        )
    else:
        source = NormalizedFileSource(args.path)
    preview = service.preview_source(source.read())
    applied = False
    if args.apply:
        service.apply_source(preview)
        applied = True
    if args.as_json:
        return _emit({**preview.as_dict(), "applied": applied})
    counts = preview.counts
    print(f"Source {preview.label} ({preview.source_id})")
    print(
        f"  {counts.get('rows', 0)} rows: {counts.get('signals', 0)} signals, {counts.get('dids', 0)} DIDs, "
        f"{counts.get('ecus', 0)} ECUs, {counts.get('endpoint_candidates', 0)} endpoint candidates"
    )
    print(
        f"  versus the current list: {counts.get('added', 0)} added, {counts.get('removed', 0)} removed, "
        f"{counts.get('unchanged', 0)} unchanged; {counts.get('conflicts', 0)} conflicting signal(s); "
        f"{counts.get('rejected', 0)} rejected"
    )
    for where, reason in preview.rejections[:5]:
        print(f"  rejected {where}: {reason}")
    if preview.blocking_problem:
        print(f"BLOCKED: {preview.blocking_problem}")
        return EXIT_FAILED
    print("Applied: candidate list and cache updated." if applied else "Preview only. Re-run with --apply to use it.")
    return EXIT_OK


def _cache_rebuild(service: OperatorService, args: argparse.Namespace) -> int:
    service.rebuild_cache()
    if args.as_json:
        return _emit({"rebuilt": True})
    print("Rebuilt the project cache from tracked files, the candidate list, and recordings.")
    return EXIT_OK


def _signals(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    tokens = list(args.selectors)
    if tokens[:1] == ["show"]:
        return _signals_show(service, tree, tokens[1:], args)
    filtered = bool(tokens or args.ecu or args.states or args.search)
    selection = service.select(
        tree, tokens, ecus=args.ecu, states=args.states, search=args.search, require=False
    )
    chosen_dids = set(selection.dids)
    chosen_signals = set(selection.signals)
    if args.as_json:
        document = tree.as_dict()
        if filtered:
            for endpoint in document["endpoints"]:
                endpoint["dids"] = [row for row in endpoint["dids"] if row["key"] in chosen_dids]
                for row in endpoint["dids"]:
                    row["signals"] = [item for item in row["signals"] if item["signal_ref"] in chosen_signals]
            document["endpoints"] = [row for row in document["endpoints"] if row["dids"] or not filtered]
        return _emit(document)
    printed = 0
    for endpoint in tree.endpoints:
        dids = [did for did in endpoint.dids if not filtered or did.key in chosen_dids]
        if filtered and not dids:
            continue
        address = f"{endpoint.tester_can_id}/{endpoint.response_can_id}" if endpoint.tester_can_id else "address unknown"
        ecus = ",".join(str(key) for key in endpoint.ecu_keys)
        print(f"{endpoint.key}  [{endpoint.state}]  {address}  ECU {ecus}  {', '.join(endpoint.ecu_names)}")
        for did in dids:
            settings = did.settings
            cadence = (
                f"  every {settings['target_interval_ms']} ms (max {settings['maximum_interval_ms']})"
                + ("" if settings["included"] else " excluded")
                if settings else ""
            )
            print(f"  {did.data_id}  {did.state}{f' ({did.detail})' if did.detail and did.state != 'Verified' else ''}{cadence}")
            for item in did.signals:
                if filtered and item.signal_ref not in chosen_signals:
                    continue
                if printed >= args.limit:
                    continue
                printed += 1
                print(f"    {item.signal_ref:<22} {item.name}{f' [{item.unit}]' if item.unit else ''}{_target_text(item)}")
    total = len(chosen_signals) if filtered else tree.counts()["signals"]
    if printed < total:
        print(f"... {total - printed} more signal(s); narrow with selectors, --ecu, --state, --search, or --limit")
    return EXIT_OK


def _signals_show(service: OperatorService, tree, tokens: list[str], args: argparse.Namespace) -> int:
    if not tokens:
        raise ValueError("usage: telemetry signals show SELECTOR")
    selection = service.select(tree, tokens)
    document: dict[str, Any] = {"dids": [], "signals": []}
    for key in selection.dids:
        found = tree.find_did(key)
        if found:
            row = found[1].as_dict()
            row.pop("signals")
            document["dids"].append(row)
    for ref in selection.signals:
        found = tree.find_signal(ref)
        if found:
            document["signals"].append(found[2].as_dict())
    if args.as_json:
        return _emit(document)
    for row in document["dids"]:
        print(f"DID {row['key']}: {row['state']} {row['detail']}".rstrip())
        if row["baseline_hex"]:
            print(f"  baseline {row['baseline_hex']}  latency {row['latency_ms']} ms  at {row['observed_utc']}")
        if row["settings"]:
            settings = row["settings"]
            print(
                f"  settings: interval {settings['target_interval_ms']} ms, max {settings['maximum_interval_ms']} ms, "
                f"priority {settings['priority']}, {'included' if settings['included'] else 'excluded'}"
            )
    for row in document["signals"]:
        print(f"Signal {row['signal_ref']}: {row['name']} [{row['unit']}] formula {row['formula']}")
        if row["conflict"]:
            print(f"  {row['variants']} catalog variants disagree; choose with `telemetry signal set --variant`")
        if row["review"]:
            print(f"  review: {row['review']['evidence_state']}; {row['review']['notes']}".rstrip("; "))
        if row["target"]:
            print(f"  target: {_requirements_text(row['target'])}")
        if row["progress"]:
            counters = row["progress"]["counters"]
            print(
                f"  progress: {counters['samples']} samples, {counters['runs']} runs, "
                f"{counters['changes']} changes, {counters['transitions']} transitions"
                + (" (complete)" if row["progress"]["complete"] else "")
            )
    return EXIT_OK


def _endpoint_add(service: OperatorService, args: argparse.Namespace) -> int:
    parts = args.address.split(":")
    if len(parts) != 3:
        raise ValueError("address must be TESTER:RESPONSE:RECIPE, e.g. 0x7E0:0x7E8:extended")
    service.add_endpoint(parts[0], parts[1], parts[2], args.ecu)
    if args.as_json:
        return _emit({"added": args.address, "ecu_keys": args.ecu})
    print(f"Added endpoint candidate {args.address} for ECU {', '.join(map(str, args.ecu))}. "
          "Verify it with `telemetry verify`.")
    return EXIT_OK


def _endpoint_confirm(service: OperatorService, args: argparse.Namespace) -> int:
    endpoint = service.confirm_endpoint(args.endpoint_id, args.ecu, args.provenance, dry_run=args.dry_run)
    if args.as_json:
        return _emit({"endpoint": endpoint, "written": not args.dry_run})
    verb = "Would confirm" if args.dry_run else "Confirmed"
    print(f"{verb} {endpoint['id']} ({endpoint['tester_can_id']}/{endpoint['response_can_id']}) "
          f"for ECU {', '.join(map(str, endpoint['ecu_keys']))} in config/transport.json.")
    return EXIT_OK


def _verify(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    selection = service.select(tree, args.selectors, ecus=args.ecu, states=args.states, search=args.search)
    try:
        scope = service.verification_scope(tree, selection, session_recipe_id=args.recipe)
    except RecipeRequired as exc:
        raise ValueError(f"{exc}; pass --recipe NAME") from exc
    repository = service.repository()
    if args.dry_run:
        record_preview(repository, "verify", scope.digest)
        if args.as_json:
            return _emit({"dry_run": True, "scope": scope.as_dict()})
        _print_verification_scope(scope)
        print("Dry run only; nothing was transmitted. Re-run without --dry-run to start.")
        return EXIT_OK
    if not args.as_json:
        _print_verification_scope(scope)
    _confirm_live(repository, "verify", scope.digest, scope.summary(), args)
    stop = threading.Event()

    def show(event: dict[str, Any]) -> None:
        if args.as_json:
            print(json.dumps(event, sort_keys=True), flush=True)
        elif event["event"] == "endpoint":
            print(f"  endpoint {event['key']}: {'responded' if event['responded'] else 'no response'}", flush=True)
        else:
            detail = f" {event['detail']}" if event.get("detail") else ""
            print(f"  {event['key']}: {event['outcome']}{detail}", flush=True)

    with _safe_interrupt(stop, "Stopping after the current probe…"):
        result = service.verify(scope, on_event=show, stop_event=stop)
    summary = result.as_dict()
    if args.as_json:
        print(json.dumps({"event": "finished", **summary}, sort_keys=True))
    else:
        outcomes = ", ".join(f"{count} {name}" for name, count in sorted(summary["outcomes"].items()))
        print(("Stopped early: " if result.stopped else "Finished: ") + (outcomes or "no DIDs read"))
    return EXIT_OK


def _print_verification_scope(scope) -> None:
    print(scope.summary())
    for row in scope.endpoints:
        action = "probe endpoint, then" if row.probe_endpoint else "use"
        print(f"  {row.key}: {action} {row.target_id} ({row.reason}); session {row.session_recipe_id}")
        print(f"    DIDs: {', '.join(row.dids)}")
    print(f"  {scope.probe_count} probe(s), at most {scope.upper_bound_seconds:.0f} s; scope {scope.digest[:12]}")


def _edit_and_save(service: OperatorService, args: argparse.Namespace, result) -> int:
    if not args.dry_run:
        service.save(review=result.review, policy=result.policy)
    impact = result.impact()
    if args.as_json:
        return _emit({"saved": not args.dry_run, **impact})
    for line in impact["changes"]:
        print(f"  {line}")
    if impact["created_did_settings"]:
        print(f"  created DID settings with defaults: {', '.join(impact['created_did_settings'])}")
    if impact["unselected_siblings"]:
        print(f"  also affects {len(impact['unselected_siblings'])} unselected signal(s) sharing these DIDs")
    noun = f"{len(impact['signals'])} signal(s)" if impact["signals"] else f"{len(impact['dids'])} DID(s)"
    print(("Dry run: would change " if args.dry_run else "Saved ") + noun + ".")
    return EXIT_OK


def _signal_set(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    selection = service.select(tree, args.selectors, ecus=args.ecu, states=args.states, search=args.search)
    fields = {
        name: value for name, value in (
            ("name", args.name), ("formula", args.formula), ("unit", args.unit),
            ("evidence_state", args.evidence_state), ("notes", args.note), ("variant", args.variant),
        ) if value is not None
    }
    if not fields:
        raise ValueError("nothing to change: give --name, --formula, --unit, --evidence, --note, or --variant")
    return _edit_and_save(service, args, service.edit_signals(tree, selection.signals, **fields))


def _did_set(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    selection = service.select(tree, args.selectors, ecus=args.ecu, states=args.states, search=args.search)
    fields: dict[str, Any] = {}
    if args.interval is not None:
        fields["target_interval_ms"] = args.interval
    if args.max_interval is not None:
        fields["maximum_interval_ms"] = args.max_interval
    if args.priority is not None:
        fields["priority"] = args.priority
    if args.include or args.exclude:
        fields["included"] = bool(args.include)
    result = service.edit_dids(tree, selection.dids, selected_signals=selection.signals, **fields)
    return _edit_and_save(service, args, result)


def _target_set(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    selection = service.select(tree, args.selectors, ecus=args.ecu, states=args.states, search=args.search)
    requirements: dict[str, Any] = {}
    for name, value in (
        ("minimum_samples", args.min_samples), ("minimum_runs", args.min_runs),
        ("minimum_transitions", args.min_transitions), ("minimum_changes", args.min_changes),
    ):
        if value is not None:
            requirements[name] = value
    if args.min_span is not None:
        requirements["minimum_span_seconds"] = parse_duration(args.min_span)
    if args.min_applicable is not None:
        requirements["minimum_applicable_seconds"] = parse_duration(args.min_applicable)
    if args.required_states is not None:
        requirements["required_states"] = [
            int(value) for value in args.required_states.split(",") if value.strip()
        ]
    fields: dict[str, Any] = {"requirements": requirements}
    if args.min_difference is not None:
        fields["minimum_difference"] = args.min_difference
    if args.when is not None:
        fields["condition"] = parse_condition(args.when, args.when_max_age)
    if args.include or args.exclude:
        fields["included"] = bool(args.include)
    return _edit_and_save(service, args, service.edit_targets(tree, selection.signals, **fields))


def _target_clear(service: OperatorService, args: argparse.Namespace) -> int:
    tree = service.tree()
    selection = service.select(tree, args.selectors, ecus=args.ecu, states=args.states, search=args.search)
    return _edit_and_save(service, args, service.clear_targets(selection.signals))


def _collect(service: OperatorService, args: argparse.Namespace) -> int:
    readiness = service.readiness(args.endpoint)
    repository = service.repository()
    if args.dry_run or not readiness.ready:
        if readiness.ready:
            record_preview(repository, "collect", readiness.digest)
        if args.as_json:
            _emit({"dry_run": True, **readiness.as_dict()})
        else:
            _print_readiness(readiness)
            if readiness.ready:
                print("Dry run only; nothing was transmitted. Re-run without --dry-run to start.")
        return EXIT_OK if readiness.ready else EXIT_FAILED
    if not args.as_json:
        _print_readiness(readiness)
    summary = (
        f"Start LIVE collection of {readiness.summary['dids']} DID(s) on {readiness.endpoint_id}"
    )
    _confirm_live(repository, "collect", readiness.digest, summary, args)
    stop = threading.Event()
    _start_stdin_stop(stop)
    reason_box: dict[str, str] = {}

    def emit(event: dict) -> None:
        parse_public_event(json.dumps(event, separators=(",", ":")))
        if args.as_json:
            print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
        elif event["event"] == "started":
            print(f"LIVE collection started: run {event['run_id']}. Ctrl+C or type 'stop' to stop safely.", flush=True)
        elif event["event"] == "progress":
            print(
                f"  {event['elapsed_ms'] // 1000}s  {event['exchanges']} reads ({event['failed_exchanges']} failed)  "
                f"targets {event['targets_complete']}/{event['targets']} complete",
                flush=True,
            )
        elif event["event"] == "finished":
            reason_box["reason"] = event["reason"]
            print(f"Collection finished: {event['reason']}", flush=True)
        elif event["event"] == "error":
            print(f"ERROR: {event['detail']}", flush=True)

    with _safe_interrupt(stop, "Stopping safely…"):
        reason = service.collect(readiness, note=args.note, event_sink=emit, stop_event=stop)
    return EXIT_OK if reason in {"operator_stop", "plan_complete", "size_limit"} else EXIT_FAILED


def _print_readiness(readiness) -> None:
    if readiness.ready:
        summary = readiness.summary
        print(f"Ready to collect on {readiness.endpoint_id}:")
        print(f"  {summary['unfinished_targets']} unfinished target(s), {summary['dids']} DID(s)")
        if summary["estimated_bus_utilization"] is not None:
            print(
                f"  estimated bus load {summary['estimated_bus_utilization'] * 100:.2f}%, "
                f"{summary['request_rate_hz']:.1f} requests/s, storage limit {summary['capture_tree_max_mib']} MiB"
            )
        print(f"  scope {readiness.digest[:12]}")
        return
    print("Not ready to collect:")
    for problem in readiness.problems:
        print(f"  - {problem.message}")
        print(f"    fix: {problem.command}")


def _recordings(service: OperatorService, args: argparse.Namespace) -> int:
    if args.action == "list":
        if args.list_runs:
            runs = service.runs()
            if args.as_json:
                return _emit({"runs": [run.as_dict() for run in runs]})
            if not runs:
                print("No recorded runs.")
            for run in runs:
                duration = f"{run.duration_seconds:.0f}s" if run.duration_seconds is not None else "-"
                print(
                    f"{run.run_id}  {run.state:<14} {run.created_utc or 'unknown time':<24} {duration:>7}  "
                    f"{run.exchanges} reads ({run.failed} failed)  {run.endpoint_id or ''}  {run.note or ''}".rstrip()
                )
            return EXIT_OK
        drives = service.trashed() if args.trash else service.drives()
        if args.as_json:
            return _emit({"trash" if args.trash else "drives": [drive.as_dict() for drive in drives]})
        if not drives:
            print("The trash is empty." if args.trash else "No recordings.")
        for drive in drives:
            print(_drive_line(drive))
        return EXIT_OK
    if args.action == "empty-trash":
        drives = service.trashed()
        if not drives:
            print("The trash is empty.")
            return EXIT_OK
        if not args.yes:
            if args.as_json:
                raise ValueError("empty-trash with --json needs --yes")
            answer = input(f"Permanently delete {len(drives)} drive(s) in the trash? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                print("Kept the trash.")
                return EXIT_OK
        removed = service.empty_trash()
        if args.as_json:
            return _emit({"deleted_drives": removed})
        print(f"Deleted {removed} drive(s) permanently.")
        return EXIT_OK
    if args.action == "export":
        tree = service.tree()
        refs = service.select(tree, args.signals).signals if args.signals else []
        if not refs:
            raise ValueError("export needs at least one --signal SELECTOR")
        run_ids = list(args.runs)
        for key in args.drives:
            run_ids.extend(service.drive(key).run_ids)
        if args.output:
            with args.output.open("w", encoding="utf-8", newline="") as stream:
                count = service.export(refs, stream, run_ids=run_ids)
            message = {"exported": count, "output": str(args.output)}
            if args.as_json:
                return _emit(message)
            print(f"Wrote {count} observation(s) to {args.output}")
            return EXIT_OK
        service.export(refs, sys.stdout, run_ids=run_ids)
        return EXIT_OK
    if not args.drive:
        raise ValueError(f"usage: telemetry recordings {args.action} DRIVE")
    if args.action == "show":
        drive = service.drive(args.drive)
        if args.as_json:
            return _emit(drive.as_dict(runs=True))
        print(_drive_line(drive))
        for run in drive.runs:
            state = run.state if run.state != "complete" else (run.end_label or run.state)
            print(f"  run {run.run_id}  {run.created_utc or 'unknown'}  {run.exchanges} reads "
                  f"({run.failed} failed), {run.observations} samples  {state}")
            if run.detail:
                print(f"    {run.detail}")
        return EXIT_OK
    if args.action == "recover":
        paths = service.recover_drive(args.drive)
        if args.as_json:
            return _emit({"recover": args.drive, "paths": [str(path) for path in paths]})
        print(f"Recovered {len(paths)} interrupted run(s); their saved reads are kept.")
        return EXIT_OK
    drive = service.trash(args.drive) if args.action == "trash" else service.restore(args.drive)
    if args.as_json:
        return _emit({args.action: drive.key, "runs": len(drive.runs)})
    print(("Moved drive {} to the trash." if args.action == "trash" else "Restored drive {}.").format(drive.key))
    return EXIT_OK


def _drive_line(drive) -> str:
    when = drive.key.replace("T", " ") + " UTC" if drive.start else f"unknown time ({drive.key})"
    length = _length(drive.duration_seconds)
    flags = f"  {len(drive.interrupted)} interrupted" if drive.interrupted else ""
    note = f"  {'; '.join(drive.notes)}" if drive.notes else ""
    return (f"{when:<22} {length:>8}  {drive.total('observations')} samples  "
            f"{', '.join(drive.endpoint_ids) or '-'}{flags}{note}")


def _length(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min"
    return f"{seconds // 3600:.0f} h {seconds % 3600 // 60:.0f} min"


def _init(args: argparse.Namespace) -> int:
    service = OperatorService.create(args.folder, args.project_id, args.profile_id)
    repository = service.repository()
    if args.as_json:
        return _emit({"project_root": str(service.root), "project_id": repository.project_id,
                      "profile_id": repository.profile_id})
    print(f"Created workspace {repository.project_id} in {service.root}")
    print(f"Next: cd {service.root} and run telemetry status")
    return EXIT_OK


def _export(service: OperatorService, args: argparse.Namespace) -> int:
    manifest = service.export_result(args.destination)
    if args.as_json:
        return _emit({"generation_id": manifest["generation_id"], "row_counts": manifest["row_counts"],
                      "destination": str(args.destination.resolve())})
    print(f"Exported result {manifest['generation_id']} with {manifest['row_counts']['signals']} signals "
          f"to {args.destination.resolve()}")
    return EXIT_OK


# Helpers ----------------------------------------------------------------


def _confirm_live(repository, command: str, digest: str, summary: str, args: argparse.Namespace) -> None:
    if args.yes:
        require_previewed(repository, command, digest)
        return
    if not sys.stdin.isatty():
        raise Refused(
            f"{command} transmits to the vehicle; run it in a terminal to confirm, or preview with "
            f"--dry-run and repeat with --yes"
        )
    print(f"{summary}?", file=sys.stderr)
    answer = input("Type 'yes' to start LIVE traffic: ")
    if answer.strip().lower() != "yes":
        raise Refused("cancelled; nothing was transmitted")


class _safe_interrupt:
    """Turn Ctrl+C into a cooperative stop; repeat presses only report it."""

    def __init__(self, stop: threading.Event, message: str) -> None:
        self.stop = stop
        self.message = message
        self.previous = None

    def __enter__(self) -> "_safe_interrupt":
        if threading.current_thread() is threading.main_thread():
            self.previous = signals_module.signal(signals_module.SIGINT, self._handle)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.previous is not None:
            signals_module.signal(signals_module.SIGINT, self.previous)

    def _handle(self, _signum, _frame) -> None:
        if self.stop.is_set():
            print("Safe stop is already in progress.", file=sys.stderr, flush=True)
        else:
            print(self.message, file=sys.stderr, flush=True)
            self.stop.set()


def _start_stdin_stop(stop: threading.Event) -> None:
    def read() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                stop.set()
                return

    threading.Thread(target=read, name="telemetry-stop-input", daemon=True).start()


def _target_text(item) -> str:
    if item.target is None:
        return ""
    text = f"  target: {_requirements_text(item.target)}"
    if item.progress is not None:
        text += " (complete)" if item.progress["complete"] else f" ({item.progress['counters']['samples']} samples)"
    return text


def _requirements_text(target: dict[str, Any]) -> str:
    parts = []
    for name, value in target["requirements"].items():
        if name.endswith("_seconds"):
            value = format_duration(value)
        elif isinstance(value, list):
            value = ",".join(map(str, value))
        parts.append(f"{REQUIREMENT_LABELS[name].lower()} {value}")
    if target.get("minimum_difference") is not None:
        parts.append(f"difference {target['minimum_difference']:g}")
    if target.get("condition") is not None:
        parts.append(f"when {format_condition(target['condition'])}")
    if not target["included"]:
        parts.append("excluded")
    return ", ".join(parts)


def _emit(document: dict[str, Any]) -> int:
    print(json.dumps(document, sort_keys=True, default=str))
    return EXIT_OK


def _error(exc: Exception, args: argparse.Namespace, code: int) -> int:
    if getattr(args, "as_json", False):
        print(json.dumps({"error": str(exc), "exit_code": code}, sort_keys=True))
    else:
        print(f"ERROR: {exc}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
