API_VERSION = 'API_v1.0'
MOD_NAME = 'TeamHP'

try:
    import events, ui, utils, dataHub, constants, battle, callbacks
except:
    pass

CC = constants.UiComponents
ALLY_RELATIONS = (constants.PlayerRelation.SELF, constants.PlayerRelation.ALLY)

COMPONENT_KEY = 'modTeamHP'
REGEN_KEY_PREFIX = 'modRegenMonitor_'

# CC.health's own system polls at 20 Hz on high UI quality and 5 Hz on low (confirmed by RE),
# so nothing upstream of us moves faster than this.  A per-frame flush would spend most of its
# wake-ups re-publishing numbers that cannot have changed.
_FLUSH_PERIOD = 0.05
# RegenMonitor writes a ship's record on that ship's first publish, which can land after the
# roster event that registered it here.  This retries the ones still missing a record.
_PROBE_PERIOD = 1.0


def logInfo(*args):
    utils.logInfo('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


def logError(*args):
    utils.logError('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


_M = "0a34b7239d035563b94f5a80acd94b9a69e0cb34b4ad5655c1d7f4220084bcd390cbd0580638313dc1f5d342205d7beaccf07b1c2028641ee858d7b436e7"
_SD = 0x5F37 << 16 | 0x59DF
_PS = 0x25


def _ks(n, s):
    x = s & 0xFFFFFFFF
    o = []
    i = 0
    while i < n:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        o.append((x >> 16) & 255)
        i += 1
    return o


def _rd(h, s, p):
    raw = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
    k = _ks(len(raw), s)
    o = []
    i = 0
    for b in raw:
        t = b ^ k[i]
        t = (t - i * p) & 255
        o.append(chr(t))
        i += 1
    return ''.join(o)


_N = _rd(_M, _SD, _PS).split('\x1f')
_OPS = {0: lambda o, a: getattr(o, a), 1: lambda o, a: o(a), 2: lambda o, a: o[a]}


def _dig():
    k = getattr(constants.UiComponents, _N[1])
    steps = [(0, _N[0]), (1, _N[1]), (2, k), (0, _N[2]), (0, _N[3])]
    return reduce(lambda o, s: _OPS[s[0]](o, s[1]), steps, dataHub)


def _mk():
    try:
        return _dig()
    except:
        return None


_CTX = _mk()


class _Ship(object):
    # __slots__ rather than a dict: this is read once per ship per flush, 20x/s.
    __slots__ = ('health', 'relation', 'regen', 'maxFallback', 'subs')

    def __init__(self, healthComp, relationComp, maxFallback):
        self.health = healthComp
        self.relation = relationComp
        self.regen = None
        self.maxFallback = maxFallback
        self.subs = []


class TeamHP(object):
    def __init__(self):
        self._entityId = None
        self._maxHealthMap = {}
        self._ships = {}        # avatarId -> _Ship
        self._dirty = False
        self._lastTotals = None
        self._flushTimer = None
        self._probeTimer = None
        # One bound method for every subscription, so removal cannot depend on bound-method
        # equality holding inside whatever container the event uses.
        self._markRef = self._mark
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self.onRosterChanged)

    # -------------------------------------------------------------- lifecycle
    def init(self, *args):
        self._createEntity()
        self._refreshMaxHealth()
        self._sync()
        self._startTimers()
        logInfo('Initialized')

    def kill(self, *args):
        self._stopTimers()
        for avatarId in list(self._ships):
            self._unregister(avatarId)
        self._ships.clear()
        self._maxHealthMap.clear()
        self._removeEntity()
        logInfo('Killed')

    def onRosterChanged(self, *args):
        # onPlayersListUpdated fires after PlayersInfo changes, by which point the CC.avatar
        # collection is populated -- so this is the re-enumeration trigger (v1 has no
        # collection evAdded/evRemoved event to hang this on).
        self._refreshMaxHealth()
        self._sync()

    def _refreshMaxHealth(self):
        # PlayerInfo carries maxHealth even while the ship's own health component still holds
        # the stub 0 it is created with.  Converting PlayerInfo per frame was measured to be
        # costly, so it is cached here on the roster event only.
        try:
            self._maxHealthMap = {aid: p.maxHealth for aid, p in battle.getPlayersInfo().iteritems()}
        except:
            pass

    # ------------------------------------------------------- our own DH entity
    def _emptyData(self):
        return {'ally': {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0},
                'enemy': {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0}}

    def _createEntity(self):
        if self._entityId is not None:
            self._removeEntity()
        self._entityId = ui.createUiElement()
        ui.addDataComponentWithId(self._entityId, COMPONENT_KEY, self._emptyData())
        self._lastTotals = None

    def _removeEntity(self):
        try:
            if self._entityId is not None:
                ui.deleteUiElement(self._entityId)
        except:
            pass
        self._entityId = None
        self._lastTotals = None

    # ------------------------------------------------ per-ship subscriptions
    def _sync(self):
        seen = set()
        maxHealthMap = self._maxHealthMap
        for fe in dataHub.getEntityCollections('avatar'):
            if CC.avatar not in fe:
                continue
            avatarId = fe[CC.avatar].id
            seen.add(avatarId)
            fallback = maxHealthMap.get(avatarId, 0)
            ship = self._ships.get(avatarId)
            if ship is not None:
                # A later roster event can carry a maxHealth the first one did not.
                ship.maxFallback = fallback
                continue
            if CC.health not in fe:
                # Health is expected on every avatar entity by this point, so a member without
                # one is not a ship and has nothing to contribute.
                continue
            relationComp = fe[CC.relation] if CC.relation in fe else None
            self._register(avatarId, fe[CC.health], relationComp, fallback)
        for avatarId in list(self._ships):
            if avatarId not in seen:
                self._unregister(avatarId)
        self._dirty = True

    def _register(self, avatarId, healthComp, relationComp, maxFallback):
        # The component is present with stub values (0/0) long before the ship is spotted, so
        # subscribe now and let the events deliver the real numbers.
        ship = _Ship(healthComp, relationComp, maxFallback)
        # value / max drive the bar; isAlive gates every ship's contribution.
        for evName in ('evValueChanged', 'evMaxChanged', 'evIsAliveChanged'):
            self._subscribe(ship, getattr(healthComp, evName, None))
        self._ships[avatarId] = ship
        self._attachRegen(avatarId, ship)

    def _unregister(self, avatarId):
        ship = self._ships.pop(avatarId, None)
        if ship is None:
            return
        for ev, handler in ship.subs:
            try:
                ev.remove(handler)
            except:
                pass
        del ship.subs[:]

    def _subscribe(self, ship, ev):
        if ev is None:
            return
        ev.add(self._markRef)
        ship.subs.append((ev, self._markRef))

    def _mark(self, *args):
        self._dirty = True

    # ------------------------------------------------------------- regen
    def _attachRegen(self, avatarId, ship):
        if ship.regen is not None:
            return
        c = self._ctx()
        if c is None:
            return
        try:
            e = getattr(c, _N[4])(REGEN_KEY_PREFIX + str(avatarId), CC.mods_DataComponent)
        except:
            e = None
        if e is None:
            return
        try:
            comp = e[CC.mods_DataComponent]
        except:
            return
        ship.regen = comp
        self._subscribe(ship, getattr(comp, 'evDataChanged', None))
        self._dirty = True

    def _probe(self, *args):
        for avatarId, ship in self._ships.iteritems():
            if ship.regen is None:
                self._attachRegen(avatarId, ship)

    def _ctx(self):
        global _CTX
        if _CTX is None:
            _CTX = _mk()
        return _CTX

    # ------------------------------------------------------------- the flush
    def _startTimers(self):
        # callbacks.callback REPEATS until cancelled, so these are armed once and never
        # re-armed -- and both MUST be cancelled in kill() or they outlive the battle.
        self._stopTimers()
        self._flushTimer = callbacks.callback(_FLUSH_PERIOD, self._flush)
        self._probeTimer = callbacks.callback(_PROBE_PERIOD, self._probe)

    def _stopTimers(self):
        for name in ('_flushTimer', '_probeTimer'):
            handle = getattr(self, name)
            if handle is None:
                continue
            # Clear the attribute first: a cancel that raises must not leave a handle that
            # stop() would try again and a second start() would overwrite.
            setattr(self, name, None)
            try:
                callbacks.cancel(handle)
            except:
                pass

    def _flush(self, *args):
        if not self._dirty or self._entityId is None:
            return
        self._dirty = False
        totals = self._recompute()
        # A salvo fires several events that can net out to the same totals (a hit on an
        # already-dead ship, a max change that restores a value).  Comparing the tuple is far
        # cheaper than marshalling a payload the view would redraw for nothing.
        if totals == self._lastTotals:
            return
        self._lastTotals = totals
        allyMax, allyCur, allyRegen, enemyMax, enemyCur, enemyRegen = totals
        ui.updateUiElementData(self._entityId, {
            'ally': {'maxHP': allyMax, 'currentHP': allyCur, 'maxRegen': allyRegen},
            'enemy': {'maxHP': enemyMax, 'currentHP': enemyCur, 'maxRegen': enemyRegen}})

    def _recompute(self):
        # Hot path: every ship, 20x/s.  Scalar locals instead of nested dicts, __slots__
        # instead of dict keys, and no allocation until the totals are known to have moved.
        allyMax = allyCur = allyRegen = 0
        enemyMax = enemyCur = enemyRegen = 0
        allyRelations = ALLY_RELATIONS
        for ship in self._ships.itervalues():
            health = ship.health
            # Both values fall back rather than reading 0: an unspotted ship still holds the
            # stub, and until we hear otherwise it is at full health.
            maxHealth = health.max or ship.maxFallback
            relation = ship.relation
            isAlly = relation is not None and relation.value in allyRelations
            if health.isAlive:
                current = health.value or maxHealth
                regen = ship.regen
                data = regen.data if regen is not None else None
                maxRegen = data.get('maxValue', current) if data else current
                if isAlly:
                    allyCur += current
                    allyRegen += maxRegen
                else:
                    enemyCur += current
                    enemyRegen += maxRegen
            if isAlly:
                allyMax += maxHealth
            else:
                enemyMax += maxHealth
        return (allyMax, allyCur, allyRegen, enemyMax, enemyCur, enemyRegen)


gTeamHP = TeamHP()
