"""Smoke test for the Watt smart-home device-command bus.

Proves the rail end-to-end::

    backend  ->  Firebase Realtime DB  ->  (streamed Unity game applies)  ->  command_results

Modes:

    # Default: one thermostat command (quick rail check)
    python manage.py smoke_smart_home --household-id TEAM1 --setpoint 20

    # Deploy real catalog blocks (exercises the Step 2 compile + batch-write path)
    python manage.py smoke_smart_home --household-id TEAM1 --blocks battery_smart,lights_auto_off

    # Connectivity only (no write)
    python manage.py smoke_smart_home --read-only
"""
import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf


class Command(BaseCommand):
    help = "Drive the live Watt smart-home game (rail + deploy smoke test)."

    def add_arguments(self, parser):
        parser.add_argument("--class-id", default=None,
                            help="Firebase class id (default: settings.WATT_HACKATHON_CLASS_ID or WATT).")
        parser.add_argument("--household-id", default="TEAM1",
                            help="Household id, i.e. the team code (default: TEAM1).")
        parser.add_argument("--blocks", default="",
                            help="Comma-separated catalog block ids to compile + deploy (Step 2 path).")
        parser.add_argument("--setpoint", type=float, default=20.0,
                            help="Thermostat setpoint for the default single-command test (16-28).")
        parser.add_argument("--ttl-ticks", type=int, default=shf.DEFAULT_TTL_TICKS)
        parser.add_argument("--timeout", type=float, default=15.0,
                            help="Seconds to wait for command_results (default 15).")
        parser.add_argument("--read-only", action="store_true",
                            help="Only read the observation (connectivity check); do not write.")
        parser.add_argument("--keep", action="store_true",
                            help="Keep the test command node(s) instead of deleting them afterwards.")

    def handle(self, *args, **opts):
        class_id = opts["class_id"] or getattr(settings, "WATT_HACKATHON_CLASS_ID", "WATT")
        household_id = opts["household_id"]
        root = shf.household_root(class_id, household_id)
        self.stdout.write(self.style.MIGRATE_HEADING(f"Household root: {root}"))

        # 1) Read the live observation (proves databaseURL + RTDB read access).
        try:
            observation = shf.read_observation(class_id, household_id)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Failed to read observation (RTDB wiring problem?): {exc}") from exc

        if not isinstance(observation, dict):
            self.stdout.write(self.style.WARNING(
                "No observation at observations/current — no game is publishing for this household. "
                "RTDB connectivity is OK, but there's no live game to drive."))
            return

        tick = shf.read_current_tick(observation)
        self.stdout.write(self.style.SUCCESS(
            f"Observation: day={observation.get('day')} tick={tick} "
            f"game_time={observation.get('game_time')} paused={observation.get('paused')} "
            f"thermostat_setpoint_c={observation.get('thermostat_setpoint_c')}"))

        published = shf.observation_published_at_ms(observation)
        age_ms = (shf.now_ms() - published) if published else None
        age_str = f"{age_ms / 1000:.1f}s" if age_ms is not None else "unknown"
        if shf.is_observation_live(observation, shf.now_ms()):
            self.stdout.write(self.style.SUCCESS(f"Observation is FRESH (age {age_str}) -> a game authority is live."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Observation is STALE (age {age_str}) -> the streamed game is likely NOT running; "
                f"the deploy endpoint would return 409 here. (Smoke tool writes anyway for diagnostics.)"))

        if opts["read_only"]:
            self.stdout.write("--read-only set; not writing. RTDB read path verified.")
            return

        # 2) Build the command batch: real block deploy, or the default thermostat nudge.
        commands = []
        labels = {}
        if opts["blocks"].strip():
            block_ids = [b.strip() for b in opts["blocks"].split(",") if b.strip()]
            unknown = [b for b in block_ids if b not in blocks.known_block_ids()]
            if unknown:
                raise CommandError(
                    f"Unknown block ids: {', '.join(unknown)}. "
                    f"Known: {', '.join(sorted(blocks.known_block_ids()))}")
            specs = blocks.compile_blocks(block_ids)
            self.stdout.write(
                f"Compiled {len(block_ids)} block(s) -> {len(specs)} command(s) (deduped by concern).")
            for spec in specs:
                command = shf.build_command(
                    spec["action"], spec["target_type"], spec["target_id"], spec["params"],
                    tick, ttl_ticks=opts["ttl_ticks"])
                commands.append(command)
                labels[command["command_id"]] = spec["block_id"]
        else:
            command = shf.build_command(
                "set_thermostat_setpoint", "thermostat", "thermostat",
                {"setpoint_c": opts["setpoint"]}, tick, ttl_ticks=opts["ttl_ticks"])
            commands.append(command)
            labels[command["command_id"]] = "thermostat_setpoint"

        self.stdout.write("Writing command(s):")
        self.stdout.write(json.dumps(commands, indent=2))
        try:
            shf.write_commands(class_id, household_id, commands)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Failed to write commands (RTDB write problem?): {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(commands)} command(s) under {root}/commands"))

        # 3) Poll command_results for each command's verdict.
        self.stdout.write(f"Polling command_results (timeout {opts['timeout']}s)...")
        pending = {command["command_id"] for command in commands}
        results = {}
        deadline = time.time() + opts["timeout"]
        while pending and time.time() < deadline:
            for command_id in list(pending):
                try:
                    result = shf.read_command_result(class_id, household_id, command_id)
                except Exception:  # noqa: BLE001
                    result = None
                if isinstance(result, dict):
                    results[command_id] = result
                    pending.discard(command_id)
            if pending:
                time.sleep(1.5)

        accepted_count = 0
        for command_id, label in labels.items():
            result = results.get(command_id)
            if result is None:
                self.stdout.write(self.style.WARNING(f"  [{label}] no result (timeout)"))
                continue
            accepted = bool(result.get("accepted"))
            accepted_count += 1 if accepted else 0
            style = self.style.SUCCESS if accepted else self.style.ERROR
            self.stdout.write(style(
                f"  [{label}] accepted={accepted} reason={result.get('reason')!r} "
                f"message={result.get('message')!r}"))

        if results:
            self.stdout.write(self.style.SUCCESS(
                f"FULL RAIL VERIFIED: {accepted_count}/{len(commands)} accepted by the live game."))
        else:
            self.stdout.write(self.style.WARNING(
                "No results within timeout — write path OK, but no Unity authority consumed them "
                "(is a game streaming for this household and unpaused?)."))

        # 4) Clean up our own test node(s) unless asked to keep them.
        if not opts["keep"]:
            for command_id in labels:
                try:
                    shf.delete_command(class_id, household_id, command_id)
                except Exception:  # noqa: BLE001
                    pass
            self.stdout.write("Cleaned up test command node(s).")
