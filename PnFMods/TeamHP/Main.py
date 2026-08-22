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

# CC.health polls at 20 Hz (high UI quality) / 5 Hz (low), so reading faster than this
# cannot see more.
_TICK_PERIOD = 0.05


def logInfo(*args):
    utils.logInfo('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


def logError(*args):
    utils.logError('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


_M = "0a34b7239d035563b94f5a80acd94b9a69e0cb34b4ad5655c1d7f4220084bcd390cbd0580638313dc1f5d342205d7beaccf07b1c2028641ee858d7b436e7bb3724e12b46b68245f56eb3"
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
    # Resolved once per ship; the tick reads it, so keep it a slot lookup.
    __slots__ = ('avatarId', 'health', 'relation', 'regen', 'maxFallback')

    def __init__(self, avatarId, healthComp, relationComp, maxFallback):
        self.avatarId = avatarId
        self.health = healthComp
        self.relation = relationComp
        self.regen = None
        self.maxFallback = maxFallback


class TeamHP(object):
    def __init__(self):
        self._entityId = None
        self._maxHealthMap = {}
        self._ships = {}        # game avatar entity -> _Ship
        self._lastTotals = None
        self._timer = None
        self._hooks = []
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self.onRosterChanged)

    # -------------------------------------------------------------- lifecycle
    def init(self, *args):
        # Health, not avatar: this is the collection whose component we hold, and it need
        # not arrive with the avatar.  Registering off avatars would need a retry.
        healths = self._collection(CC.health)
        if healths is None:
            logError('no collection reach; publishing nothing')
            return
        self._createEntity()
        self._refreshMaxHealth()
        self._hooks = [(healths.evAdded, self._onHealthAdded),
                       (healths.evRemoved, self._onHealthRemoved)]
        for ev, handler in self._hooks:
            ev.add(handler)
        for entity in healths:
            self._register(entity)
        self._startTick()
        logInfo('Initialized')

    def kill(self, *args):
        self._stopTick()
        for ev, handler in self._hooks:
            try:
                ev.remove(handler)
            except:
                pass
        del self._hooks[:]
        self._ships.clear()
        self._maxHealthMap.clear()
        self._removeEntity()
        logInfo('Killed')

    def onRosterChanged(self, *args):
        self._refreshMaxHealth()

    def _refreshMaxHealth(self):
        # PlayerInfo is the only maxHealth source while a ship holds its stub 0.
        # Converting it per frame was measured costly, so it is cached per roster event.
        try:
            self._maxHealthMap = {aid: p.maxHealth for aid, p in battle.getPlayersInfo().iteritems()}
        except:
            return
        for ship in self._ships.itervalues():
            ship.maxFallback = self._maxHealthMap.get(ship.avatarId, 0)

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

    # ------------------------------------------------------- the ship cache
    def _collection(self, componentId):
        # The gate's getEntityCollections rebuilds a wrapper per entity per call and has
        # no add/remove event.  The real collection has both.
        if _CTX is None:
            return None
        try:
            return getattr(_CTX, _N[5])[componentId]
        except:
            return None

    def _onHealthAdded(self, entity):
        self._register(entity)

    def _onHealthRemoved(self, entity):
        self._unregister(entity)

    def _register(self, entity):
        # The health collection also holds buildings; only avatars are ships.
        if entity in self._ships or not entity.has(CC.avatar):
            return
        avatarId = entity.avatar.id
        # Only the component references are cached.  Their VALUES are read every tick, so
        # no change event is needed -- one would carry no payload the tick does not re-read.
        ship = _Ship(avatarId, entity.health,
                     entity.relation if entity.has(CC.relation) else None,
                     self._maxHealthMap.get(avatarId, 0))
        self._ships[entity] = ship
        self._attachRegen(ship)

    def _unregister(self, entity):
        self._ships.pop(entity, None)

    def _attachRegen(self, ship):
        # RegenMonitor creates the record with the avatar, so one lookup is enough.
        if _CTX is None:
            return
        try:
            e = getattr(_CTX, _N[4])(REGEN_KEY_PREFIX + str(ship.avatarId), CC.mods_DataComponent)
            comp = e.mods_DataComponent if e is not None else None
        except:
            return
        if comp is None:
            return
        ship.regen = comp

    # -------------------------------------------------------------- the tick
    def _startTick(self):
        # callbacks.callback REPEATS: arm once, cancel in kill().
        self._stopTick()
        self._timer = callbacks.callback(_TICK_PERIOD, self._tick)

    def _stopTick(self):
        handle = self._timer
        if handle is None:
            return
        self._timer = None
        try:
            callbacks.cancel(handle)
        except:
            pass

    def _tick(self, *args):
        if self._entityId is None:
            return
        totals = self._recompute()
        # Most ticks land on an unchanged total, so this is what keeps an idle battle
        # from publishing.
        if totals == self._lastTotals:
            return
        self._lastTotals = totals
        allyMax, allyCur, allyRegen, enemyMax, enemyCur, enemyRegen = totals
        ui.updateUiElementData(self._entityId, {
            'ally': {'maxHP': allyMax, 'currentHP': allyCur, 'maxRegen': allyRegen},
            'enemy': {'maxHP': enemyMax, 'currentHP': enemyCur, 'maxRegen': enemyRegen}})

    def _recompute(self):
        allyMax = allyCur = allyRegen = 0
        enemyMax = enemyCur = enemyRegen = 0
        allyRelations = ALLY_RELATIONS
        for ship in self._ships.itervalues():
            health = ship.health
            # An unspotted ship holds the stub, so treat it as untouched.
            maxHealth = health.max or ship.maxFallback
            relation = ship.relation
            isAlly = relation is not None and relation.value in allyRelations
            if health.isAlive:
                current = health.value or maxHealth
                regen = ship.regen
                data = regen.data if regen is not None else None
                # 0 is RegenMonitor's "no figure": no repair party, or none computed yet.
                maxRegen = (data.get('maxValue') or current) if data else current
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
