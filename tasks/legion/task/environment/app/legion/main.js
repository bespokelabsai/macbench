// LEGION starter bot: trains workers and gathers. It never builds or attacks,
// so it cannot win. Replace it with your own AI.
//
// Contract: this file must export LegionBot, a BaseAI subclass. Other modules
// may live next to it in /app/legion/ and are imported as
// "simulation/ai/legion/bot/<file>.js".
import { BaseAI } from "simulation/ai/common-api/baseAI.js";
import { ResourcesManager } from "simulation/ai/common-api/resources.js";

const TARGET_WORKERS = 40;

export function LegionBot(settings)
{
	BaseAI.call(this, settings);
}

LegionBot.prototype = Object.create(BaseAI.prototype);

LegionBot.prototype.CustomInit = function(gameState)
{
	this.civ = gameState.getPlayerCiv();
};

LegionBot.prototype.OnUpdate = function(sharedScript)
{
	// Think every fifth turn (once per simulated second).
	if (this.turn++ % 5 !== 0)
		return;
	const gameState = this.gameState;
	this.trainWorkers(gameState);
	this.sendIdleWorkersToGather(gameState);
};

LegionBot.prototype.trainWorkers = function(gameState)
{
	const workers = gameState.getOwnUnits().toEntityArray().filter(ent => ent.hasClass("Worker"));
	if (workers.length >= TARGET_WORKERS || gameState.getPopulation() >= gameState.getPopulationLimit())
		return;
	const resources = gameState.getResources();
	for (const cc of gameState.getOwnStructures().toEntityArray())
	{
		if (!cc.hasClass("CivCentre") || cc.foundationProgress() !== undefined || (cc.trainingQueue() || []).length > 1)
			continue;
		const options = (cc.trainableEntities(this.civ) || [])
			.map(name => gameState.getTemplate(name))
			.filter(template => template && template.hasClass("Worker"));
		// Prefer the cheapest worker the civilization offers.
		options.sort((a, b) => a.costSum() - b.costSum());
		const choice = options.find(template => resources.canAfford(new ResourcesManager(template.cost())));
		if (choice)
			cc.train(this.civ, choice.templateName(), 1);
	}
};

LegionBot.prototype.sendIdleWorkersToGather = function(gameState)
{
	const idle = gameState.getOwnUnits().toEntityArray().filter(ent => ent.hasClass("Worker") && ent.isIdle() && ent.position());
	if (!idle.length)
		return;
	const supplies = {};
	for (const type of ["food", "wood", "stone", "metal"])
		supplies[type] = gameState.getResourceSupplies(type).toEntityArray().filter(ent => ent.position() && !ent.hasClass("Animal"));
	idle.forEach((worker, index) => {
		// Two in three gather food, the rest wood.
		const type = index % 3 === 2 ? "wood" : "food";
		const target = nearest(worker.position(), supplies[type]) || nearest(worker.position(), supplies.wood);
		if (target)
			worker.gather(target);
	});
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
