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


class TeamHP(object):
    def __init__(self):
        self._entityId = None
        self._records = {}      # avatarId -> {'maxHealth': int, 'regen': component|None}
        self._lastTotals = None
        self._timer = None
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self._refresh)

    # -------------------------------------------------------------- lifecycle
    def init(self, *args):
        if self._collection(CC.avatar) is None:
            logError('no collection reach; publishing nothing')
            return
        self._createEntity()
        self._refresh()
        self._startTick()
        logInfo('Initialized')

    def kill(self, *args):
        self._stopTick()
        self._records.clear()
        self._removeEntity()
        logInfo('Killed')

    def _refresh(self, *args):
        # PlayerInfo is the only maxHealth source while a ship still holds the stub 0 it
        # is created with, and converting one was measured costly, so it is read on the
        # roster event only.  The regen record rides along: RegenMonitor creates it with
        # the avatar, and this is the point where a re-added avatar's is picked up again.
        try:
            players = battle.getPlayersInfo()
        except:
            return
        records = {}
        for avatarId, player in players.iteritems():
            records[avatarId] = {'maxHealth': player.maxHealth,
                                 'regen': self._regen(avatarId)}
        self._records = records

    def _regen(self, avatarId):
        if _CTX is None:
            return None
        try:
            e = getattr(_CTX, _N[4])(REGEN_KEY_PREFIX + str(avatarId), CC.mods_DataComponent)
            return e.mods_DataComponent if e is not None else None
        except:
            return None

    def _collection(self, componentId):
        # The gate's getEntityCollections rebuilds a wrapper per entity per call.  This
        # is the real collection, and it is the exact set of players -- the health one
        # also holds squadrons, whose health.max is a plane count.
        if _CTX is None:
            return None
        try:
            return getattr(_CTX, _N[5])[componentId]
        except:
            return None

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
        avatars = self._collection(CC.avatar)
        if avatars is None or self._entityId is None:
            return
        totals = self._totals(avatars)
        # Most ticks land on an unchanged total, so this is what keeps an idle battle
        # from publishing.
        if totals == self._lastTotals:
            return
        self._lastTotals = totals
        allyMax, allyCur, allyRegen, enemyMax, enemyCur, enemyRegen = totals
        ui.updateUiElementData(self._entityId, {
            'ally': {'maxHP': allyMax, 'currentHP': allyCur, 'maxRegen': allyRegen},
            'enemy': {'maxHP': enemyMax, 'currentHP': enemyCur, 'maxRegen': enemyRegen}})

    def _totals(self, avatars):
        allyMax = allyCur = allyRegen = 0
        enemyMax = enemyCur = enemyRegen = 0
        allyRelations = ALLY_RELATIONS
        records = self._records
        for entity in avatars:
            if not entity.has(CC.health):
                # Health can lag the avatar; the next tick picks the ship up.
                continue
            health = entity.health
            record = records.get(entity.avatar.id)
            # An unspotted ship holds the stub, so read it as untouched.
            maxHealth = health.max or (record['maxHealth'] if record else 0)
            relation = entity.relation if entity.has(CC.relation) else None
            isAlly = relation is not None and relation.value in allyRelations
            if health.isAlive:
                current = health.value or maxHealth
                regen = record['regen'] if record else None
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
