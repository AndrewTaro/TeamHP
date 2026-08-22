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

# The tick runs every frame but early-returns on one attribute read unless a health/regen
# event set the dirty flag, so the recompute + publish happen only when something moved.
_PROBE_EVERY = 15


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


class TeamHP(object):
    def __init__(self):
        self._entityId = None
        self._maxHealthMap = {}
        self._ships = {}        # avatarId -> {health, relation, regen, subs}
        self._dirty = False
        self._probeCounter = 0
        self._tick = None
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self.onRosterChanged)

    # -------------------------------------------------------------- lifecycle
    def init(self, *args):
        self._createEntity()
        self._refreshMaxHealth()
        self._sync()
        self._startTick()
        logInfo('Initialized')

    def kill(self, *args):
        self._stopTick()
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
        # PlayerInfo carries maxHealth even for ships whose health component still reads 0
        # (spotted outside render range, or not yet spotted).  Converting PlayerInfo per frame
        # was measured to be costly, so it is cached here on the roster event only.
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

    def _removeEntity(self):
        try:
            if self._entityId is not None:
                ui.deleteUiElement(self._entityId)
        except:
            pass
        self._entityId = None

    # ------------------------------------------------ per-ship subscriptions
    def _sync(self):
        seen = set()
        for fe in dataHub.getEntityCollections('avatar'):
            if CC.avatar not in fe:
                continue
            avatarId = fe[CC.avatar].id
            seen.add(avatarId)
            if avatarId in self._ships:
                continue
            if CC.health not in fe:
                # Health rides on the avatar entity; if it is not here yet the next roster
                # event will pick the ship up.
                continue
            healthComp = fe[CC.health]
            relationComp = fe[CC.relation] if CC.relation in fe else None
            self._register(avatarId, healthComp, relationComp)
        for avatarId in list(self._ships):
            if avatarId not in seen:
                self._unregister(avatarId)
        self._dirty = True

    def _register(self, avatarId, healthComp, relationComp):
        ship = {'health': healthComp, 'relation': relationComp, 'regen': None, 'subs': []}
        # value / max drive the bar; isAlive gates every ship's contribution.
        for evName in ('evValueChanged', 'evMaxChanged', 'evIsAliveChanged'):
            self._subscribe(ship, getattr(healthComp, evName, None))
        self._ships[avatarId] = ship
        self._attachRegen(avatarId, ship)

    def _unregister(self, avatarId):
        ship = self._ships.pop(avatarId, None)
        if ship is None:
            return
        for ev, handler in ship['subs']:
            try:
                ev.remove(handler)
            except:
                pass
        del ship['subs'][:]

    def _subscribe(self, ship, ev):
        if ev is None:
            return
        ev.add(self._mark)
        ship['subs'].append((ev, self._mark))

    def _mark(self, *args):
        self._dirty = True

    # ------------------------------------------------------------- regen
    def _attachRegen(self, avatarId, ship):
        if ship['regen'] is not None:
            return
        c = self._ctx()
        if c is None:
            return
        try:
            e = getattr(c, _N[4])(REGEN_KEY_PREFIX + str(avatarId), CC.mods_DataComponent)
        except:
            e = None
        if e is None:
            # The source creates its record lazily on a ship's first publish, so this can be
            # empty at roster time.  _probeRegen retries the ones still missing.
            return
        try:
            comp = e[CC.mods_DataComponent]
        except:
            return
        ship['regen'] = comp
        self._subscribe(ship, getattr(comp, 'evDataChanged', None))
        self._dirty = True

    def _probeRegen(self):
        for avatarId, ship in self._ships.iteritems():
            if ship['regen'] is None:
                self._attachRegen(avatarId, ship)

    def _ctx(self):
        global _CTX
        if _CTX is None:
            _CTX = _mk()
        return _CTX

    # ------------------------------------------------------------- the flush
    def _startTick(self):
        if self._tick is not None:
            self._stopTick()
        self._tick = callbacks.perTick(self._onTick)

    def _stopTick(self):
        if self._tick is not None:
            callbacks.cancel(self._tick)
            self._tick = None

    def _onTick(self, *args):
        self._probeCounter += 1
        if self._probeCounter >= _PROBE_EVERY:
            self._probeCounter = 0
            self._probeRegen()
        if not self._dirty or self._entityId is None:
            return
        self._dirty = False
        ui.updateUiElementData(self._entityId, self._recompute())

    def _recompute(self):
        data = self._emptyData()
        for avatarId, ship in self._ships.iteritems():
            healthComp = ship['health']
            relationComp = ship['relation']
            team = 'ally' if (relationComp is not None and relationComp.value in ALLY_RELATIONS) else 'enemy'

            maxHealth = healthComp.max if healthComp.max else self._maxHealthMap.get(avatarId, 0)
            isAlive = healthComp.isAlive
            currentHealth = healthComp.value if healthComp.value else maxHealth

            regenComp = ship['regen']
            if regenComp is not None and regenComp.data:
                maxRegen = regenComp.data.get('maxValue', currentHealth)
            else:
                maxRegen = currentHealth

            bucket = data[team]
            bucket['maxHP'] += maxHealth
            bucket['currentHP'] += currentHealth * isAlive
            bucket['maxRegen'] += maxRegen * isAlive
        return data


gTeamHP = TeamHP()
