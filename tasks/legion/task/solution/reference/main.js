// LEGION reference AI: an original, deliberately simple rush AI.
// It trains workers, builds houses, trains citizen soldiers and attacks the
// nearest enemy in waves. It proves the score is reachable; it says nothing
// about collaboration.
import { BaseAI } from "simulation/ai/common-api/baseAI.js";
import { ResourcesManager } from "simulation/ai/common-api/resources.js";

const TARGET_CIVILIANS = 18;
const WAVE_SIZE = 14;
const WAVE_INTERVAL_TURNS = 300;

export function LegionBot(settings)
{
	BaseAI.call(this, settings);
}

LegionBot.prototype = Object.create(BaseAI.prototype);

LegionBot.prototype.CustomInit = function(gameState)
{
	this.civ = gameState.getPlayerCiv();
	this.lastWave = -WAVE_INTERVAL_TURNS;
	this.houseFoundations = 0;
};

LegionBot.prototype.OnUpdate = function(sharedScript)
{
	this.turn++;
	if (this.turn % 5 !== 0)
		return;
	const gameState = this.gameState;
	const units = gameState.getOwnUnits().toEntityArray();
	const soldiers = units.filter(ent => ent.hasClass("Soldier"));
	const civilians = units.filter(ent => ent.hasClass("Civilian"));

	this.train(gameState, civilians.length < TARGET_CIVILIANS ? "Civilian" : "Infantry");
	this.buildHouse(gameState, civilians);
	this.gather(gameState, units.filter(ent => ent.hasClass("Worker") && ent.isIdle() && ent.position() &&
		!(ent.hasClass("Soldier") && ent.getMetadata(PlayerID, "wave"))));

	const ready = soldiers.filter(ent => !ent.getMetadata(PlayerID, "wave"));
	if (ready.length >= WAVE_SIZE && this.turn - this.lastWave >= WAVE_INTERVAL_TURNS)
		this.launchWave(gameState, ready);
	this.retarget(gameState, soldiers.filter(ent => ent.getMetadata(PlayerID, "wave") && ent.isIdle()));
};

LegionBot.prototype.train = function(gameState, preferredClass)
{
	if (gameState.getPopulation() >= gameState.getPopulationLimit())
		return;
	const resources = gameState.getResources();
	for (const building of gameState.getOwnStructures().toEntityArray())
	{
		if (!building.hasClass("CivCentre") && !building.hasClass("Barracks"))
			continue;
		if (building.foundationProgress() !== undefined || (building.trainingQueue() || []).length > 1)
			continue;
		const options = (building.trainableEntities(this.civ) || [])
			.map(name => gameState.getTemplate(name))
			.filter(t => t && (t.hasClass(preferredClass) || t.hasClass("Worker")));
		options.sort((a, b) => (b.hasClass(preferredClass) - a.hasClass(preferredClass)) || (a.costSum() - b.costSum()));
		const choice = options.find(t => resources.canAfford(new ResourcesManager(t.cost())));
		if (choice)
			building.train(this.civ, choice.templateName(), choice.hasClass("Soldier") ? 3 : 1);
	}
};

LegionBot.prototype.buildHouse = function(gameState, civilians)
{
	// Finish any house already placed before placing another.
	const foundation = gameState.getOwnFoundations().toEntityArray().find(ent => ent.hasClass("House"));
	if (foundation)
	{
		const helpers = civilians.filter(ent => ent.position()).slice(0, 3);
		for (const helper of helpers)
			if (helper.unitAIOrderData().every(order => order.target !== foundation.id()))
				helper.repair(foundation);
		return;
	}
	if (gameState.getPopulationLimit() - gameState.getPopulation() > 8 || gameState.getPopulationLimit() >= gameState.getPopulationMax())
		return;
	const cc = gameState.getOwnStructures().toEntityArray().find(ent => ent.hasClass("CivCentre"));
	const builder = civilians.find(ent => ent.position());
	if (!cc || !builder)
		return;
	const name = (builder.buildableEntities(this.civ) || []).find(n => {
		const t = gameState.getTemplate(n);
		return t && t.hasClass("House");
	});
	if (!name)
		return;
	const template = gameState.getTemplate(name);
	if (!gameState.getResources().canAfford(new ResourcesManager(template.cost())))
		return;
	// Try a ring of spots around the civic centre; the simulation rejects bad ones.
	const position = cc.position();
	const angle = (this.houseFoundations++ * 2.39996) % (2 * Math.PI);
	const radius = 28 + 4 * (this.houseFoundations % 5);
	builder.construct(name, position[0] + radius * Math.cos(angle), position[1] + radius * Math.sin(angle), Math.PI * 0.75);
};

LegionBot.prototype.gather = function(gameState, idle)
{
	if (!idle.length)
		return;
	const supplies = {};
	for (const type of ["food", "wood"])
		supplies[type] = gameState.getResourceSupplies(type).toEntityArray().filter(ent => ent.position() && !ent.hasClass("Animal"));
	idle.forEach((worker, index) => {
		const type = index % 2 === 0 ? "food" : "wood";
		const target = nearest(worker.position(), supplies[type]) || nearest(worker.position(), supplies.wood);
		if (target)
			worker.gather(target);
	});
};

LegionBot.prototype.launchWave = function(gameState, soldiers)
{
	const target = this.pickTarget(gameState, soldiers[0].position());
	if (!target)
		return;
	this.lastWave = this.turn;
	for (const soldier of soldiers)
	{
		soldier.setMetadata(PlayerID, "wave", this.turn);
		soldier.attackMove(target[0], target[1], { "attack": ["Unit", "Structure"] });
	}
};

LegionBot.prototype.retarget = function(gameState, idleRaiders)
{
	if (!idleRaiders.length)
		return;
	const target = this.pickTarget(gameState, idleRaiders[0].position());
	if (target)
		for (const soldier of idleRaiders)
			soldier.attackMove(target[0], target[1], { "attack": ["Unit", "Structure"] });
};

LegionBot.prototype.pickTarget = function(gameState, from)
{
	if (!from)
		return undefined;
	const enemies = gameState.getEnemyStructures().toEntityArray().filter(ent => ent.position());
	const pool = enemies.length ? enemies : gameState.getEnemyUnits().toEntityArray().filter(ent => ent.position());
	const target = nearest(from, pool);
	return target && target.position();
};

function nearest(position, entities)
{
	let best;
	let bestDistance = Infinity;
	for (const ent of entities)
	{
		const p = ent.position();
		const d = (p[0] - position[0]) ** 2 + (p[1] - position[1]) ** 2;
		if (d < bestDistance)
		{
			bestDistance = d;
			best = ent;
		}
	}
	return best;
}
