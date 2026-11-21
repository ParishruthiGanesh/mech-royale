"""Team Inventors' Mech Royale bot (protocol mechfight/1).

Legs and torso are decided separately every observation:
  legs  - zone safety > dodging shells > retreat to cover > fight dance > loot > roam
  torso - aim (with lead) and fire at the best visible target, else look around
All rule numbers come from hello.config / the observation; the map comes from hello.map.
"""
from __future__ import annotations

import gc
import math
import random

import mechfight as mf
from world import Search, World

PI = math.pi
STYLE = "berserk"  # "normal", or for experiments: "pacifist" (never shoots mechs, always hides) / "berserk" (never retreats)
RANGE = "far"  # fight distance: "far" (FAR_M, scored best), "auto" (by loadout) or "close" (35 m)
FAR_M = 150.0
PURSUE = False  # walk to where a mech was last seen instead of roaming
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1}
if RANGE == "close":
    WEAPON_RANK = {"lightning": 3, "laser": 2, "mortar": 1}
UPGRADE_ORDER = [
    "plating", "armor", "laser_damage", "gun_damage", "nanites", "laser_charge", "gun_rate",
    "lightning_efficiency", "servos", "gun_velocity", "gyros", "gun_mag", "lightning_chain",
    "reactor", "lightning_range", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
    "sensors", "jets", "laser_pierce",
]


def wrap(a: float) -> float:
    return (a + PI) % (2 * PI) - PI


def intercept(dx, dy, dz, vx, vy, vz, speed):
    """Time for a straight shot at `speed` to meet a target at offset d moving at v (0 if none)."""
    a = vx * vx + vy * vy + vz * vz - speed * speed
    b = 2 * (dx * vx + dy * vy + dz * vz)
    c = dx * dx + dy * dy + dz * dz
    if abs(a) < 1e-6:
        return -c / b if b < 0 else 0.0
    disc = b * b - 4 * a * c
    if disc < 0:
        return 0.0
    r = math.sqrt(disc)
    t1, t2 = (-b - r) / (2 * a), (-b + r) / (2 * a)
    ts = [t for t in (t1, t2) if t > 0]
    return min(ts) if ts else 0.0


class Bot:
    name = "inventors"

    def __init__(self, hello: mf.Hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(hello.get("seed", 1))
        self.take_r = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]
        self.size_per_cube = cfg["cubes"].get("size_per_cube", 0.05)
        self.mech_h = cfg["mech"]["height"]
        self.mech_r = cfg["mech"]["radius"]
        self.npc_h = cfg["npc"].get("height", 3.5)
        self.npc_r = cfg["npc"].get("radius", 1.8)
        self.crate_h = cfg["crates"].get("height", 2.4)
        self.crate_r = cfg["crates"].get("radius", 1.4)
        self.world = None
        try:
            self.world = World(hello["map"]["dir"], hello["map"].get("files", []))
        except Exception as e:  # no map: steer straight, still a legal bot
            mf.log(f"map not loaded ({e!r}); navigating blind")
        self.tracks = {}  # id -> dict for mechs and NPCs
        self.pickups = {}  # id -> (kind, pos, t_seen)
        self.crates = {}  # id -> (pos, t_seen)
        self.bad_pickups = set()
        self.path = None
        self.path_goal = None
        self.path_t = -1e9
        self.search = None
        self.search_goal = None
        self.strafe = 1.0
        self.strafe_until = 0.0
        self.retreat = False
        self.cover = None
        self.cover_t = -1e9
        self.look_yaw = None
        self.look_until = -1.0
        self.roam = None
        self.roam_t = -1e9
        self.stuck_t = 0.0
        self.unstick_until = -1.0
        self.unstick_dir = (0.0, 0.0)
        self.last_threat_t = -1e9
        self.last_hurt_t = -1e9
        self.laser_hold_t = None
        gc.collect()
        gc.freeze()

    # ------------------------------------------------------------------ navigation
    def nav(self, x, y, gx, gy, t):
        """Unit move vector that walks around buildings toward (gx, gy)."""
        w = self.world
        dx, dy = gx - x, gy - y
        d = math.hypot(dx, dy)
        if d < 0.5:
            return 0.0, 0.0
        if w is None or (d < 60 and w.walk_clear(x, y, gx, gy, 3.0)):
            return dx / d, dy / d
        pg = self.path_goal
        need = self.path is None or pg is None or math.hypot(pg[0] - gx, pg[1] - gy) > max(8.0, 0.15 * d) or t - self.path_t > 4.0
        if need and self.search is None:
            a, b = w.nearest_free(x, y), w.nearest_free(gx, gy)
            if a >= 0 and b >= 0:
                self.search, self.search_goal = Search(w, a, b), (gx, gy)
        if self.search is not None and self.search.step(700):
            if self.search.path:
                self.path = self.search.path
            elif need:
                self.path = None
            self.path_goal, self.path_t, self.search = self.search_goal, t, None
        path = self.path
        if not path:
            return dx / d, dy / d
        # drop waypoints already reached, then head for the furthest one in a straight clear walk
        while len(path) > 1 and math.hypot(path[0][0] - x, path[0][1] - y) < 3.5:
            path.pop(0)
        best = 0
        for k in range(min(len(path) - 1, 7), 0, -1):
            if w.walk_clear(x, y, path[k][0], path[k][1], 3.0):
                best = k
                break
        if best > 0:
            del path[:best]
        tx, ty = path[0]
        dd = math.hypot(tx - x, ty - y)
        if len(path) == 1 and dd < 2.0 and not w.free(gx, gy):
            return 0.0, 0.0  # the goal itself is inside a wall: this is as close as it gets
        if dd < 1e-6:
            return dx / d, dy / d
        return (tx - x) / dd, (ty - y) / dd

    def hidden_from(self, px, py, threats):
        w = self.world
        return w is not None and all(not w.los(e["pos"][0], e["pos"][1], px, py) for e in threats)

    def find_cover(self, x, y, threats, zone_ok):
        """Nearest walkable spot no threat can see, or None."""
        w = self.world
        if w is None or not threats:
            return None
        best, bs = None, 1e18
        ex, ey = threats[0]["pos"][0], threats[0]["pos"][1]
        for r in (6.0, 12.0, 20.0, 30.0, 42.0, 56.0):
            n = 8 if r < 10 else 14
            off = self.rng.random() * 2 * PI
            for k in range(n):
                a = off + 2 * PI * k / n
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not w.free(px, py) or not zone_ok(px, py) or not self.hidden_from(px, py, threats):
                    continue
                s = r + (0.0 if w.walk_clear(x, y, px, py, 3.0) else 0.6 * r) - 0.25 * math.hypot(px - ex, py - ey)
                if s < bs:
                    best, bs = (px, py), s
            if best is not None and r >= 20.0:
                break
        return best

    # ------------------------------------------------------------------ aiming
    def aim_at(self, mx, my, mz, tr, speed, t_lead=None):
        """[yaw, pitch], distance and time of flight to hit track `tr` from muzzle (mx, my, mz)."""
        px, py, pz = tr["pos"]
        pz += tr["ch"]
        dx, dy, dz = px - mx, py - my, pz - mz
        if speed is None:
            tof = t_lead or 0.0
            vx, vy, vz = tr["vel"]
        else:
            vx, vy, vz = tr["vel"]
            tof = intercept(dx, dy, dz, vx, vy, 0.0, speed)
            if tof > 0.3:  # far: targets jink, so lead by their average velocity instead
                vx, vy, vz = tr["ev"]
                tof = intercept(dx, dy, dz, vx, vy, 0.0, speed)
        if abs(vz) > 3.0:
            vz *= 0.5
        else:
            vz = 0.0
        dx, dy, dz = dx + vx * tof, dy + vy * tof, dz + vz * tof
        h = math.hypot(dx, dy)
        return [math.atan2(dy, dx), math.atan2(dz, max(h, 1e-6))], h, tof

    def mortar_aim(self, mx, my, mz, tr, st):
        """Low-arc ballistic solution with lead, or None when out of reach."""
        v, g = st.get("speed", 72.0), st.get("gravity", 22.0)
        px, py, pz = tr["pos"]
        vx, vy, _ = tr["ev"]
        tof = 0.0
        sol = None
        for _ in range(3):
            dx, dy, dz = px + vx * tof - mx, py + vy * tof - my, pz - mz
            d = math.hypot(dx, dy)
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1e-3:
                return None
            th = math.atan2(v * v - math.sqrt(disc), g * d)
            tof = d / (v * math.cos(th))
            sol = ([math.atan2(dy, dx), th], d, tof)
        return sol

    # ------------------------------------------------------------------ the decision
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        st = me["stats"]
        w = self.world
        rng = self.rng
        act: mf.Action = {"tick": obs["tick"]}
        hp, max_hp = me["hp"], max(1.0, me["max_hp"])
        hpf = hp / max_hp
        energy = me["energy"]
        walk = st.get("walk_speed", 11.7)
        weapons = me["weapons"]
        kinds = [wp["kind"] if wp else None for wp in weapons]

        # ---- remember what we see
        seen_now = set()
        for m in obs["mechs"]:
            i = m["id"]
            seen_now.add(i)
            tr = self.tracks.get(i)
            v = m["vel"]
            if tr is None:
                tr = self.tracks[i] = {"ev": list(v)}
            else:
                ev = tr["ev"]
                tr["ev"] = [ev[0] * 0.7 + v[0] * 0.3, ev[1] * 0.7 + v[1] * 0.3, ev[2] * 0.7 + v[2] * 0.3]
            npc = m["kind"] == "npc"
            scale = 1.0 + self.size_per_cube * m.get("cubes", 0)
            tr.update(id=i, pos=m["pos"], vel=v, t=t, npc=npc, hp=m.get("hp", 1400.0), max_hp=m.get("max_hp", 1400.0),
                      weapons=m.get("weapons", ()), charging=m.get("charging", False),
                      ch=(self.npc_h * 0.5 if npc else self.mech_h * 0.5 * scale),
                      r=(self.npc_r if npc else self.mech_r * scale))
        for i in [i for i, tr in self.tracks.items() if t - tr["t"] > (3.0 if tr["npc"] else 8.0)]:
            del self.tracks[i]
        for p in obs["pickups"]:
            self.pickups[p["id"]] = (p["kind"], p["pos"], t)
        for c in obs["crates"]:
            self.crates[c["id"]] = (c["pos"], t)
        vis_crates = {c["id"] for c in obs["crates"]}
        vis_pick = {p["id"] for p in obs["pickups"]}
        for i in [i for i, (pos, ts) in self.crates.items() if i not in vis_crates and math.hypot(pos[0] - x, pos[1] - y) < 35]:
            del self.crates[i]  # we are close and it is no longer listed: destroyed
        for i in [i for i, (k, pos, ts) in self.pickups.items() if i not in vis_pick and math.hypot(pos[0] - x, pos[1] - y) < 30]:
            del self.pickups[i]

        for e in obs["events"]:
            et = e["type"]
            if et == "damaged":
                if e.get("weapon") != "zone":
                    self.last_hurt_t = t
                if e.get("bearing") is not None:
                    self.look_yaw, self.look_until = e["bearing"], t + 1.5
            elif et == "take_failed" and e.get("reason") in ("gone", "have"):
                self.bad_pickups.add(e["id"])
                self.pickups.pop(e["id"], None)
            elif et == "took":
                self.pickups.pop(e["id"], None)

        threats = sorted((tr for tr in self.tracks.values() if not tr["npc"] and tr["id"] in seen_now),
                         key=lambda tr: math.hypot(tr["pos"][0] - x, tr["pos"][1] - y))
        ghosts = [tr for tr in self.tracks.values() if not tr["npc"] and tr["id"] not in seen_now]
        npcs = [tr for tr in self.tracks.values() if tr["npc"] and tr["id"] in seen_now]
        if threats:
            self.last_threat_t = t

        # ---- zone
        zone = obs["zone"]
        zc, zr = zone["center"], zone["radius"]
        nc, nr = zone["next_center"], zone["next_radius"]
        d_cur = math.hypot(x - zc[0], y - zc[1])
        d_next = math.hypot(x - nc[0], y - nc[1])
        margin = min(15.0, nr * 0.3)
        over = d_next - (nr - margin)
        outside = (not me["in_zone"]) or d_cur > zr - 4.0
        must_go = over > 0 and t + over / walk * 1.4 + 8.0 >= zone["shrink_start"]
        shrinking = t >= zone["shrink_start"] - 6.0

        def zone_ok(px, py):
            if math.hypot(px - zc[0], py - zc[1]) > zr - 6.0:
                return False
            return not shrinking or math.hypot(px - nc[0], py - nc[1]) <= nr - min(4.0, nr * 0.2) or nr < 8

        # ---- pick the target for the torso
        mh = st.get("muzzle_height", 7.0)
        mx, my, mz = x, y, z + mh

        def can_see(tr):
            px, py, pz = tr["pos"]
            return w is None or abs(pz - z) > 6.0 or w.los(x, y, px, py)

        target = None
        best_score = 1e18
        for tr in threats:  # the threat we can finish soonest: low HP and close, with a clear line
            if can_see(tr):
                sc = tr["hp"] * (1.0 + math.hypot(tr["pos"][0] - x, tr["pos"][1] - y) / 120.0)
                if sc < best_score:
                    target, best_score = tr, sc
        farm_ok = hpf > 0.75 and not threats and t - self.last_threat_t > 3.0
        farm = None
        if target is None:
            near_npcs = [tr for tr in npcs if can_see(tr)]
            if near_npcs:
                tr = min(near_npcs, key=lambda tr: math.hypot(tr["pos"][0] - x, tr["pos"][1] - y))
                d = math.hypot(tr["pos"][0] - x, tr["pos"][1] - y)
                if (farm_ok and d < 90) or (d < 50 and t - self.last_hurt_t < 2.0 and not threats):
                    farm = tr
            if farm is None and farm_ok and self.crates:
                best, bd = None, 70.0
                for cid, (pos, ts) in self.crates.items():
                    d = math.hypot(pos[0] - x, pos[1] - y)
                    if d < bd and cid in vis_crates and abs(pos[2] - z) < 6 and (w is None or w.los(x, y, pos[0], pos[1])):
                        best, bd = pos, d
                if best is not None:
                    farm = {"id": -1, "pos": best, "vel": (0.0, 0.0, 0.0), "ev": (0.0, 0.0, 0.0), "ch": self.crate_h * 0.5,
                            "r": self.crate_r, "npc": False, "hp": 300.0, "weapons": (), "crate": True}
        shoot = (target if STYLE != "pacifist" else None) or farm

        # ---- torso: aim and triggers
        fire = [False, False]
        aim = None
        label = "roam"
        if shoot is not None:
            sd = math.hypot(shoot["pos"][0] - x, shoot["pos"][1] - y)
            cur_yaw, cur_pitch = me["aim"]
            gun_aim = None
            for s, wp in enumerate(weapons):
                if wp is None:
                    continue
                k, ws = wp["kind"], wp["stats"]
                if k == "gun":
                    a, h, tof = self.aim_at(mx, my, mz, shoot, ws.get("speed", 252.0))
                    gun_aim = a
                    if aim is None:
                        aim = a
                    tol = math.atan2(shoot["r"] * 0.9, max(h, 1.0)) + 0.015
                    if (h < ws.get("range", 230.0) * 0.97 and abs(wrap(a[0] - cur_yaw)) < tol
                            and abs(a[1] - cur_pitch) < tol + 0.02 and wp.get("reload", 0) <= 0):
                        fire[s] = True
                elif k == "lightning":
                    rngm = ws.get("range", 45.0)
                    cone = math.radians(ws.get("cone_deg", 30.0)) * 0.45
                    yaw_to = math.atan2(shoot["pos"][1] - y, shoot["pos"][0] - x)
                    if sd < rngm - 1.0 and abs(wrap(yaw_to - cur_yaw)) < cone and energy > ws.get("energy", 3.0) + 2:
                        fire[s] = True
                elif k == "laser":
                    a, h, _ = self.aim_at(mx, my, mz, shoot, None, 0.08)
                    charge = wp.get("charge", 0.0)
                    full = ws.get("charge", 0.75)
                    in_range = h < ws.get("range", 280.0) * 0.97
                    tol = math.atan2(shoot["r"] * 0.7, max(h, 1.0)) + 0.004
                    lined = abs(wrap(a[0] - cur_yaw)) < tol and abs(a[1] - cur_pitch) < tol + 0.01
                    if charge > 0:
                        if self.laser_hold_t is None:
                            self.laser_hold_t = t
                        if charge >= full - 0.11:
                            aim = a  # line the beam up for the release
                        # hold until full and lined up; never hold into the automatic discharge
                        release = charge >= full - 0.01 and (lined or t - self.laser_hold_t > full + ws.get("hold_s", 2.0) - 0.5)
                        fire[s] = not release
                        if release:
                            aim = a
                    else:
                        self.laser_hold_t = None
                        if wp.get("cooldown", 0) <= 0.15 and in_range and not shoot.get("crate"):
                            fire[s] = True
                elif k == "mortar":
                    if wp.get("cooldown", 0) <= 0.2 and 14.0 < sd < 110.0 and not shoot.get("crate") and not shoot["npc"]:
                        sol = self.mortar_aim(mx, my, z + st.get("height", 9.0) + 0.5, shoot, ws)
                        if sol is not None:
                            a = sol[0]
                            aim = a
                            if abs(wrap(a[0] - cur_yaw)) < 0.03 and abs(a[1] - cur_pitch) < 0.03:
                                fire[s] = True
            if aim is None:
                aim = gun_aim or [math.atan2(shoot["pos"][1] - y, shoot["pos"][0] - x), 0.0]
            label = f"shoot {shoot['id']}"
        else:
            if "laser" in kinds:
                # a charge in progress with no target: keep holding a while in case they reappear
                s = kinds.index("laser")
                wp = weapons[s]
                if wp.get("charge", 0) > 0:
                    if self.laser_hold_t is None:
                        self.laser_hold_t = t
                    fire[s] = t - self.laser_hold_t < wp["stats"].get("charge", 0.75) + 1.2
                else:
                    self.laser_hold_t = None
            for s, wp in enumerate(weapons):
                if wp and wp["kind"] == "gun" and wp.get("ammo", 99) < wp["stats"].get("mag", 12) and wp.get("reload", 0) <= 0:
                    if not threats and (not ghosts or wp.get("ammo", 0) <= 3):
                        act["reload"] = True

        # ---- legs
        move = None
        boost = False
        jump = False

        # retreat state (hysteresis)
        near = [tr for tr in threats if math.hypot(tr["pos"][0] - x, tr["pos"][1] - y) < 160]
        danger = near + [g for g in ghosts if t - g["t"] < 6.0 and math.hypot(g["pos"][0] - x, g["pos"][1] - y) < 120]
        if not self.retreat:
            if danger and target is not None:
                weak = hpf < 0.42 and target["hp"] > hp * 0.75
                swarmed = len(near) >= 2 and hpf < 0.7
                if weak or swarmed:
                    self.retreat = True
            elif danger and hpf < 0.5:
                self.retreat = True
        else:
            if hpf > 0.85 or (not danger and t - self.last_threat_t > 8.0) or (target is not None and target["hp"] < 200 and hpf > 0.2):
                self.retreat = False
        if STYLE == "pacifist":
            self.retreat = bool(danger)
        elif STYLE == "berserk":
            self.retreat = False
        if must_go or outside:
            self.cover = None

        # mortar shells about to land on us
        flee_shell = None
        g = self.cfg["weapons"]["mortar"].get("gravity", 22.0)
        splash = self.cfg["weapons"]["mortar"].get("splash_radius", 7.0) * 1.3 + st.get("radius", 2.5) + 2
        for p in obs["projectiles"]:
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            dz = pz - z
            disc = vz * vz + 2 * g * max(dz, 0.0)
            tl = (vz + math.sqrt(disc)) / g
            if tl > 2.5:
                continue
            lx, ly = px + vx * tl, py + vy * tl
            dd = math.hypot(lx - x, ly - y)
            if dd < splash:
                flee_shell = ((x - lx) / dd, (y - ly) / dd) if dd > 0.5 else (-vy / (math.hypot(vx, vy) + 1e-6), vx / (math.hypot(vx, vy) + 1e-6))

        drop_goal = None
        bd = 150.0 if threats else 280.0
        for dr in obs["drops"]:
            pos = dr["pos"]
            d = math.hypot(pos[0] - x, pos[1] - y)
            if dr.get("holder") not in (None, me["id"]) and dr.get("progress", 0) > 1.5 and d > 30:
                continue  # someone else has nearly got it
            if d < bd and d / walk < dr["lands_in"] + 20.0 and zone_ok(pos[0], pos[1]):
                drop_goal, bd = dr, d
        near_cube = None
        if hpf > 0.3:
            cd = 30.0
            for c in obs["cubes"]:
                d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if d < cd and abs(c["pos"][2] - z) < 4.0 and zone_ok(c["pos"][0], c["pos"][1]):
                    near_cube, cd = c["pos"], d

        if outside:
            move = self.nav(x, y, nc[0], nc[1], t)
            boost = True
            label += " |zone!"
        elif flee_shell is not None:
            move = flee_shell
            boost = True
            label += " |shell"
        elif must_go:
            gx, gy = nc[0], nc[1]
            if d_next > 1 and nr > 20:
                gx, gy = nc[0] + (x - nc[0]) / d_next * nr * 0.55, nc[1] + (y - nc[1]) / d_next * nr * 0.55
            move = self.nav(x, y, gx, gy, t)
            boost = over / walk > max(0.0, zone["shrink_end"] - t) * 0.7 or (bool(threats) and energy > 50)
            label += " |zone"
        elif drop_goal is not None and (danger or hpf < 0.95 or drop_goal["lands_in"] < 12.0 or bd < 60.0):
            pos, ring = drop_goal["pos"], drop_goal.get("radius", 8.0)
            if bd < ring - 2.0:  # inside the ring: keep moving in a small circle while it captures
                move = self.nav(x, y, pos[0] + ring * 0.45 * math.cos(t * 1.3), pos[1] + ring * 0.45 * math.sin(t * 1.3), t)
            else:
                move = self.nav(x, y, pos[0], pos[1], t)
                boost = energy > 40 and (bool(danger) or drop_goal["lands_in"] <= 0)
            label += " |drop"
        elif self.retreat and danger:
            hid = self.hidden_from(x, y, danger[:3])
            closest = min(math.hypot(e["pos"][0] - x, e["pos"][1] - y) for e in danger)
            if hid and closest > 35:
                move = (0.0, 0.0)  # hold still behind cover and heal
                self.cover = None
            else:
                if self.cover is None or t - self.cover_t > 0.5 or not self.hidden_from(self.cover[0], self.cover[1], danger[:3]):
                    self.cover = self.find_cover(x, y, danger[:3], zone_ok)
                    self.cover_t = t
                if self.cover is not None:
                    move = self.nav(x, y, self.cover[0], self.cover[1], t)
                else:  # nothing to hide behind: run away, zigzagging
                    e = danger[0]
                    ax, ay = x - e["pos"][0], y - e["pos"][1]
                    ad = math.hypot(ax, ay) + 1e-6
                    move = self.fight_step(x, y, ax / ad, ay / ad, 1.0, t, obs, zone_ok, zc)
                boost = energy > 15
            label += " |retreat"
        elif near_cube is not None and (target is not None or danger):
            move = self.nav(x, y, near_cube[0], near_cube[1], t)
            label += " |cube"
        elif target is not None:
            td = math.hypot(target["pos"][0] - x, target["pos"][1] - y)
            tw = target["weapons"]
            if "lightning" in kinds and energy > 25:
                want = 30.0
            elif "lightning" in tw:
                want = 75.0
            elif "laser" in kinds and "laser" not in tw:
                want = 125.0
            else:
                want = 60.0
            if RANGE == "close":
                want = 35.0 if "lightning" not in kinds else 28.0
            elif RANGE == "far":
                want = FAR_M
            if target["hp"] < 300 and hpf > 0.3:
                want = min(want, 40.0)
            bx, by = (target["pos"][0] - x) / (td + 1e-6), (target["pos"][1] - y) / (td + 1e-6)
            radial = max(-1.0, min(1.0, (td - want) / 15.0))
            move = self.fight_step(x, y, bx, by, radial, t, obs, zone_ok, zc)
            boost = energy > 60 and abs(radial) > 0.9 and ("lightning" in kinds or RANGE == "close")
            label += " |fight"
        else:
            goal = self.loot_goal(obs, x, y, z, t, kinds, hpf, zone_ok, farm, ghosts)
            if PURSUE and ghosts and hpf > 0.4 and not obs["cubes"]:
                gtr = min(ghosts, key=lambda tr: t - tr["t"])
                if zone_ok(gtr["pos"][0], gtr["pos"][1]):
                    goal = (gtr["pos"][0], gtr["pos"][1])
            if goal is None:
                if (self.roam is None or t - self.roam_t > 30.0 or math.hypot(self.roam[0] - x, self.roam[1] - y) < 8.0
                        or math.hypot(self.roam[0] - nc[0], self.roam[1] - nc[1]) > max(nr - 10.0, 0.0)):
                    a, r = rng.random() * 2 * PI, nr * 0.65 * math.sqrt(rng.random())
                    rx, ry = nc[0] + r * math.cos(a), nc[1] + r * math.sin(a)
                    if w is not None:
                        c = w.nearest_free(rx, ry)
                        if c >= 0:
                            rx, ry = w.center(c)
                    self.roam, self.roam_t = (rx, ry), t
                goal = self.roam
                if hpf < 0.8 and t - self.last_threat_t < 15.0 and not shrinking:
                    goal = None  # hurt and nobody in sight: stand still (silent) and heal
            move = self.nav(x, y, goal[0], goal[1], t) if goal else (0.0, 0.0)

        # ---- unstick: commanded to move but not moving
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if move and (abs(move[0]) + abs(move[1])) > 0.3 and speed < 1.2:
            self.stuck_t += 0.1
        else:
            self.stuck_t = 0.0
        if self.stuck_t > 0.8 and t > self.unstick_until:
            a = rng.random() * 2 * PI
            self.unstick_dir, self.unstick_until, self.stuck_t = (math.cos(a), math.sin(a)), t + 0.7, 0.0
            self.path = None
        if t < self.unstick_until:
            move = (move[0] * 0.5 + self.unstick_dir[0], move[1] * 0.5 + self.unstick_dir[1])
            jump = bool(me["grounded"])
            boost = True

        # ---- torso when there is nothing to shoot: look where trouble is, else sweep ahead
        if aim is None:
            if ghosts and t - self.last_threat_t < 6.0:
                gtr = min(ghosts, key=lambda tr: t - tr["t"])
                aim = [math.atan2(gtr["pos"][1] - y, gtr["pos"][0] - x), 0.0]
            elif self.look_yaw is not None and t < self.look_until:
                aim = [self.look_yaw, 0.0]
            elif move and (abs(move[0]) + abs(move[1])) > 0.3:
                aim = [math.atan2(move[1], move[0]) + 1.0 * math.sin(t * 1.6), 0.0]
            else:
                aim = [wrap(t * 1.4), 0.0]

        # ---- loot interactions
        if obs["offer"]:
            choices = obs["offer"]["choices"]
            rank = {u: i for i, u in enumerate(UPGRADE_ORDER)}
            act["pick"] = min(range(len(choices)), key=lambda i: rank.get(choices[i]["id"], 99))
        slot1_rank = WEAPON_RANK.get(kinds[1], 0) if len(kinds) > 1 else 0
        for p in obs["pickups"]:
            if p["id"] in self.bad_pickups or p["kind"] in kinds or WEAPON_RANK.get(p["kind"], 0) <= slot1_rank:
                continue
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.take_r - 0.3 and abs(p["pos"][2] - z) < 3.7:
                if len(kinds) > 1 and kinds[1] is not None:
                    if kinds[1] == "laser" and weapons[1].get("charge", 0) > 0:
                        continue
                    act["drop"] = 1
                act["take"] = p["id"]
                break

        n = math.hypot(move[0], move[1])
        if n > 1.0:
            move = (move[0] / n, move[1] / n)
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["aim"] = [round(wrap(aim[0]), 4), round(max(st.get("min_pitch", -0.7), min(st.get("max_pitch", 1.3), aim[1])), 4)]
        act["fire"] = fire
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        mf.draw.label(f"{label} hp{hp:.0f}")
        return act

    # ------------------------------------------------------------------ helpers for legs
    def fight_step(self, x, y, bx, by, radial, t, obs, zone_ok, zc):
        """Strafe across the line to the enemy (bx, by), flipping at random, sidestepping bullets and walls."""
        rng = self.rng
        if t > self.strafe_until:
            if rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = t + rng.uniform(0.3, 0.9)
        # incoming bullets: step off their line
        ox = oy = 0.0
        me = obs["self"]
        cz = me["pos"][2] + 4.5
        rad = me["stats"].get("radius", 2.5) + 1.8
        for p in obs["projectiles"]:
            if p["kind"] == "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            vv = vx * vx + vy * vy + vz * vz
            if vv < 1.0:
                continue
            tc = ((x - px) * vx + (y - py) * vy + (cz - pz) * vz) / vv
            if tc < 0 or tc > 0.7:
                continue
            qx, qy = x - (px + vx * tc), y - (py + vy * tc)
            miss = math.hypot(qx, qy)
            if miss < rad:
                if miss > 0.3:
                    ox, oy = ox + qx / miss, oy + qy / miss
                else:
                    vh = math.hypot(vx, vy) + 1e-6
                    ox, oy = ox - vy / vh * self.strafe, oy + vx / vh * self.strafe
        w = self.world
        on = math.hypot(ox, oy)
        cands = []
        if on > 0.3:
            cands.append((ox / on + bx * radial * 0.3, oy / on + by * radial * 0.3))
        for s in (self.strafe, -self.strafe):
            cands.append((-by * s + bx * radial * 0.8, bx * s + by * radial * 0.8))
        cands.append((bx * radial, by * radial))
        for k, (mx, my) in enumerate(cands):
            n = math.hypot(mx, my)
            if n < 1e-6:
                continue
            mx, my = mx / n, my / n
            px, py = x + mx * 6.0, y + my * 6.0
            if (w is None or (w.free(px, py) and w.free(x + mx * 3.0, y + my * 3.0))) and zone_ok(px, py):
                if on <= 0.3 and k == 1:  # the preferred strafe side was blocked: remember the flip
                    self.strafe = -self.strafe
                return mx, my
        d = math.hypot(zc[0] - x, zc[1] - y) + 1e-6
        return (zc[0] - x) / d, (zc[1] - y) / d

    def loot_goal(self, obs, x, y, z, t, kinds, hpf, zone_ok, farm, ghosts):
        """Where to walk when nobody is in sight: cubes, a better weapon, a supply drop, a crate or NPC."""
        w = self.world

        def reachable(pos, limit):
            if abs(pos[2] - (w.ground(pos[0], pos[1]) if w else z)) > 3.5 and abs(pos[2] - z) > 3.5:
                return False  # on a roof: skip
            return math.hypot(pos[0] - x, pos[1] - y) < limit and zone_ok(pos[0], pos[1])

        best, bd = None, 1e9
        for c in obs["cubes"]:
            d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
            if d < bd and reachable(c["pos"], 90.0):
                best, bd = c["pos"], d
        if best is not None:
            return best
        slot1_rank = WEAPON_RANK.get(kinds[1], 0) if len(kinds) > 1 else 0
        for pid, (kind, pos, ts) in self.pickups.items():
            if pid in self.bad_pickups or kind in kinds:
                continue
            gain = WEAPON_RANK.get(kind, 0) - slot1_rank
            if gain <= 0:
                continue
            d = math.hypot(pos[0] - x, pos[1] - y) / (1.0 + 0.5 * gain)
            if d < bd and reachable(pos, 220.0 if slot1_rank == 0 else 110.0):
                best, bd = pos, d
        if best is not None:
            return best
        for dr in obs["drops"]:  # nothing better to do: wait at a drop that is still falling
            pos = dr["pos"]
            d = math.hypot(pos[0] - x, pos[1] - y)
            if d < 280 and zone_ok(pos[0], pos[1]) and d < bd:
                best, bd = pos, d
        if best is not None:
            return best
        if farm is not None:
            fx, fy = farm["pos"][0], farm["pos"][1]
            d = math.hypot(fx - x, fy - y)
            keep = 40.0 if farm["npc"] else 14.0
            if d > keep + 4:
                return (fx, fy)
            if farm["npc"]:  # orbit the drone so its slow bullets miss
                s = self.strafe
                if t > self.strafe_until:
                    self.strafe, self.strafe_until = -s if self.rng.random() < 0.5 else s, t + self.rng.uniform(0.6, 1.4)
                px, py = x - (fy - y) / (d + 1e-6) * 10 * s, y + (fx - x) / (d + 1e-6) * 10 * s
                if (w is None or w.free(px, py)) and zone_ok(px, py):
                    return (px, py)
            return (x, y)
        if hpf > 0.75:
            for cid, (pos, ts) in self.crates.items():
                d = math.hypot(pos[0] - x, pos[1] - y)
                if d < bd and reachable(pos, 150.0):
                    best, bd = pos, d
        return best


if __name__ == "__main__":
    mf.run(Bot)
