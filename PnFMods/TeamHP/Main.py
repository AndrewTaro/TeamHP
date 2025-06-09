API_VERSION = 'API_v1.0'
MOD_NAME = 'TeamHP'

try:
    import events, ui, utils, dataHub, constants, battle, callbacks
except:
    pass

from EntityController import EntityController

ALLY_RELATIONS = (constants.PlayerRelation.SELF, constants.PlayerRelation.ALLY)
CC = constants.UiComponents

COMPONENT_KEY = 'modTeamHP'

def logInfo(*args):
    data = [str(i) for i in args]
    utils.logInfo( '[{}] {}'.format(MOD_NAME, ', '.join(data)) )

def logError(*args):
    data = [str(i) for i in args]
    utils.logError( '[{}] {}'.format(MOD_NAME, ', '.join(data)) )


class TeamHP(object):
    def __init__(self):
        self._vary = None
        self._maxHealthMap = {}
        self._entity = EntityController(COMPONENT_KEY)
        events.onBattleShown(self.init)
        events.onBattleEnd(self.kill)
        events.onPlayersListUpdated(self.updateMaxHealthMap)

    def init(self, *args):
        self._entity.createEntity()
        self.updateMaxHealthMap()
        self.__startVary()
        logInfo('Initialized')

    def kill(self, *args):
        self.__stopVary()
        self._maxHealthMap.clear()
        self._entity.removeEntity()
        logInfo('Killed')

    def updateMaxHealthMap(self, *args):
        self._maxHealthMap = {avatarId: player.maxHealth for avatarId, player in battle.getPlayersInfo().iteritems()}

    def __startVary(self):
        if self._vary is not None:
            self.__stopVary()
        self._vary = callbacks.perTick(self.update)

    def __stopVary(self):
        callbacks.cancel(self._vary)
        self._vary = None

    def __createDataDict(self):
        return {
            'ally'  : {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0},
            'enemy' : {'maxHP': 0, 'currentHP': 0, 'maxRegen': 0}
        }

    def update(self, *args):
        data = self.__createDataDict()
        for entity in dataHub.getEntityCollections('avatar'):
            team = 'ally' if CC.relation in entity and entity[CC.relation].value in ALLY_RELATIONS else 'enemy'
            healthComp = entity[CC.health]
            
            # Health
            if not healthComp:
                logError('Health component does not exist.', entity.id)


            # Vehicle have comp until it is spotted.
            # Even then, if it is spotted outside the rendering range, health.max and value remain at 0.
            #
            # From testing, it seems covnerting PlayeInfo object into API_v_1_0.dummy object is very costly and affects the performance badly
            # Thus, calling `getPlayerInfo` every frame on every player is not recommended
            # 
            maxHealth       = healthComp.max if healthComp.max else self._maxHealthMap.get(entity[CC.avatar].id, 0)

            isAlive = healthComp.isAlive
            currentHealth = healthComp.value if healthComp.value else maxHealth

            # Regen
            if CC.dataComponent in entity:
                maxRegen = entity[CC.dataComponent].data.get('maxValue', currentHealth)
            else:
                maxRegen = currentHealth

            data[team]['maxHP'] += maxHealth
            data[team]['currentHP'] += currentHealth * isAlive
            data[team]['maxRegen'] += maxRegen * isAlive

        self._entity.updateEntity(data)


gTeamHP = TeamHP()