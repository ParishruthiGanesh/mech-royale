# Team Inventors — Mech Royale bot

Entry for the Builders Buildathon "Mech Royale" track (protocol `mechfight/1`, scorer `builders-3`).
Python 3.13, standard library only. Started from the kit's Python starter.

| File | What it is |
| --- | --- |
| `Dockerfile` | The image the scorer builds. Base image pinned by digest; nothing else is downloaded. |
| `bot.py` | The bot: targeting, firing, fight / retreat / loot / zone decisions. |
| `world.py` | Map model from `/arena`: walk and sight grids, line checks, time-sliced A*. |
| `mechfight.py` | The kit's wrapper, unchanged (stdin/stdout loop, newest-observation rule). |
| `tools/` | Dev-only helpers (not in the image): `mapcheck.py`, `sim.py`. |

## How it plays

Legs and torso are decided separately each observation.

- **Zone:** always heads for the next circle early enough to be inside before the shrink starts;
  boosts if it is ever outside. Every cover spot, strafe step and loot goal must be inside the zone.
- **Navigation:** buildings, the stadium, low bridges and steep terrain are rasterised to a 2 m grid;
  paths are A* on a 4 m grid, run in small slices so no decision is slow. Jumps/jets when stuck.
- **Fighting:** leads the gun (average velocity at long range), charges and releases the laser on
  target, arcs lightning in range, lobs the mortar with a ballistic solution. Strafes with random
  flips, sidesteps visible bullets and incoming shells, keeps a range that suits both loadouts,
  and always faces the attacker (rear armor).
- **Disengage:** below ~42% HP against a healthier enemy (or outnumbered) it breaks line of sight
  behind the nearest building and waits for regeneration; re-engages above 85%.
- **Loot:** cubes first, then a better second weapon (laser > lightning > mortar), supply drops,
  crates and NPC drones when healthy and unobserved. Upgrade picks favor HP and armor.

## Checks run (2026-11-21, kit `mechfight-kit.tar.gz`)

Run from the kit folder, with this repository at `../mech-royale`.

| Command | Result |
| --- | --- |
| `python3 check/check.py -- python3 -u python/example_bot.py` (unmodified starter) | PASS, 300/300 in time, max 0.8 ms |
| starter image under the match limits (command below with `starter-bot`) | PASS, 300/300, max 2.8 ms |
| `python3 check/check.py -- python3 -u ../mech-royale/bot.py` | PASS, 300/300, p50 0.3 / p99 2.8 / max 3.0 ms |
| `docker build --platform linux/amd64 -t inventors-bot ../mech-royale` | builds in seconds, amd64 |
| `python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 --pids-limit 128 -v "$PWD/arena:/arena:ro" inventors-bot` (3 runs) | PASS ×3, 300/300 in time, ready in ~0.45 s, p50 0.6–0.7 / p99 3.6–4.9 / max 4.4–6.9 ms |
| `tools/sim.py` in a `--cpus 1 --memory 512m --read-only --user 65534` container, 5 full sandbox matches | 0 exceptions, per decision p50 0.05 / p99 ≤ 3.0 / max 4.8 ms |

Budget is 50 ms per decision, so the worst measured answer uses about 14% of it.

## Limitations

- **Placement is unmeasured.** The kit has no match runner. `tools/sim.py` is a home-made sandbox
  with invented physics and opponents; it only catches crashes, navigation dead-ends and slow
  decisions. Real placement needs an official practice submission.
- Everything is 2D: it does not climb roofs on purpose and skips rooftop loot.
- Line of sight ignores trees and low walls; canopy is not modelled.
- Sounds and beams are not used; unseen attackers are only noticed through damage bearings.
- Fight ranges, retreat thresholds and weapon ranking are untuned guesses.

## License

MIT, see `LICENSE`.
