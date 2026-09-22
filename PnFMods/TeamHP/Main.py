API_VERSION = 'API_v1.0'
MOD_NAME = 'TeamHP'

try:
    import events, ui, utils, constants, battle, callbacks
except:
    pass

# Not in the guard above: that one swallows the absence of the INJECTED modules.
# Hub.py ships beside this file, so a failure here is a packaging fault and must
# be loud at load rather than a NameError in the first battle.
import Hub

CC = constants.UiComponents
ALLY_RELATIONS = (constants.PlayerRelation.SELF, constants.PlayerRelation.ALLY)

COMPONENT_KEY = 'modTeamHP'
REGEN_KEY_PREFIX = 'modRegenMonitor_'

# CC.health polls at 20 Hz (high UI quality) / 5 Hz (low), so reading faster than this
# cannot see more.
TICK_PERIOD = 0.05


def logInfo(*args):
    utils.logInfo('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


def logError(*args):
    utils.logError('[{}] {}'.format(MOD_NAME, ', '.join(str(i) for i in args)))


class TeamHP(object):
    def __init__(self):
        self._entityId = None
        self._playerRecords = {}    # avatarId -> {'maxHealth': int, 'regen': comp|None}
        self._lastTotals = None
        self._timer = None
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self._updatePlayerRecords)

    # -------------------------------------------------------------- lifecycle
    def init(self, *args):
        if Hub.collection(CC.avatar) is None:
            logError('no collection reach; publishing nothing')
            return
        self._createEntity()
        self._updatePlayerRecords()
        self._startTick()
        logInfo('Initialized')

    def kill(self, *args):
        self._stopTick()
        self._playerRecords.clear()
        self._removeEntity()
        logInfo('Killed')

    # ----------------------------------------------------------- the records
    def _updatePlayerRecords(self, *args):
        # PlayerInfo is the only maxHealth source while a ship still holds the stub 0 it
        # is created with, and converting one is costly, so it is read on the
        # roster event only.  Updated in place: 'regen' resolves lazily and must survive.
        try:
            players = battle.getPlayersInfo()
        except:
            return
        records = self._playerRecords
        for avatarId, player in players.iteritems():
            record = records.get(avatarId)
            if record is None:
                records[avatarId] = {'maxHealth': player.maxHealth}
            else:
                record['maxHealth'] = player.maxHealth

    def _getRegen(self, record, avatarId):
        # Key presence, not a None test -- None is a real answer.  RegenMonitor is an
        # optional install, so retrying a miss would poll the index for every ship, every
        # tick, for the whole battle.
        if 'regen' not in record:
            record['regen'] = self._getRegenComponent(avatarId)
        return record['regen']

    def _getRegenComponent(self, avatarId):
        return Hub.component(REGEN_KEY_PREFIX + str(avatarId))

    # ------------------------------------------------------- our own DH entity
    def _createDataDict(self):
        return {'ally': {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0},
                'enemy': {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0}}

    def _createEntity(self):
        if self._entityId is not None:
            self._removeEntity()
        self._entityId = ui.createUiElement()
        ui.addDataComponentWithId(self._entityId, COMPONENT_KEY, self._createDataDict())
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
        self._timer = callbacks.callback(TICK_PERIOD, self.onTick)

    def _stopTick(self):
        handle = self._timer
        if handle is None:
            return
        self._timer = None
        try:
            callbacks.cancel(handle)
        except:
            pass

    def onTick(self, *args):
        # CC.avatar is the exact set of players; the health collection also holds
        # squadrons, whose health.max is a plane count.
        avatars = Hub.collection(CC.avatar)
        if avatars is None or self._entityId is None:
            return
        totals = self._calcTeamTotals(avatars)
        # Most ticks land on an unchanged total, so this is what keeps an idle battle from
        # publishing.
        if totals == self._lastTotals:
            return
        self._lastTotals = totals
        allyMax, allyCur, allyRegen, enemyMax, enemyCur, enemyRegen = totals
        ui.updateUiElementData(self._entityId, {
            'ally': {'maxHP': allyMax, 'currentHP': allyCur, 'maxRegen': allyRegen},
            'enemy': {'maxHP': enemyMax, 'currentHP': enemyCur, 'maxRegen': enemyRegen}})

    def _calcTeamTotals(self, avatars):
        allyMax = allyCur = allyRegen = 0
        enemyMax = enemyCur = enemyRegen = 0
        allyRelations = ALLY_RELATIONS
        records = self._playerRecords
        for entity in avatars:
            if not entity.has(CC.health):
                # Health can lag the avatar; the next tick picks the ship up.
                continue
            health = entity.health
            avatarId = entity.avatar.id
            record = records.get(avatarId)
            # An unspotted ship holds the stub, so read it as untouched.
            maxHealth = health.max or (record['maxHealth'] if record else 0)
            relation = entity.relation if entity.has(CC.relation) else None
            isAlly = relation is not None and relation.value in allyRelations
            if health.isAlive:
                current = health.value or maxHealth
                regen = self._getRegen(record, avatarId) if record else None
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
