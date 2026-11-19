// LEGION referee, simulation side. The match runner replaces the time limit below.
const LEGION_TIME_LIMIT_MS = 3600000;
// Petra runs under a random AI directory per match; the match runner sets its name here.
const LEGION_PETRA_AI = "petra";
// Gathering rate per player number for Petra levels above Very Hard (Very Hard
// play, faster gathering); the match runner sets it. Other Petras use the table below.
const LEGION_PETRA_RATE_OVERRIDE = {};
// The Elite levels also build and train faster and have stronger units and structures:
// {player: {time, strength}}, set by the match runner.
const LEGION_PETRA_BOOST = {};
// Extra gathering for one player; set only for the verifier's oracle ceiling check.
const LEGION_CANDIDATE_BOOST = null;

// Petra's own difficulty handicap (simulation/ai/petra/config.js), applied here
// instead of by Petra so that no AI needs write access to the simulation.
// Sandbox, Very Easy, Easy, Medium, Hard, Very Hard.
const LEGION_PETRA_RATE = [0.42, 0.56, 0.75, 1.00, 1.25, 1.56];
const LEGION_PETRA_TIME = [1.40, 1.25, 1.10, 1.00, 1.00, 1.00];

// At the game-time limit the match runner decides the match on military score: the value
// (resource cost) of the enemy units each player killed and the enemy structures it destroyed.
Trigger.prototype.LegionTimeLimit = function()
{
	const destroyed = {};
	const numPlayers = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers();
	for (let player = 1; player < numPlayers; ++player)
	{
		const cmpStats = QueryPlayerIDInterface(player, IID_StatisticsTracker);
		const s = cmpStats ? cmpStats.GetStatistics() : {};
		destroyed[player] = Math.round((+s.enemyUnitsKilledValue || 0) + (+s.enemyBuildingsDestroyedValue || 0));
	}
	warn("LEGION_TIEBREAK " + JSON.stringify(destroyed));
	warn("LEGION_TIME_LIMIT " + Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime());
};

{
	const cmpPlayerManager = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager);
	const numPlayers = cmpPlayerManager.GetNumPlayers();
	const playerData = InitAttributes.settings.PlayerData;
	// PlayerData may or may not carry an entry for Gaia (player 0).
	const offset = playerData.length >= numPlayers ? 0 : 1;
	for (let player = 1; player < numPlayers; ++player)
	{
		const data = playerData[player - offset];
		if (!data || data.AI !== LEGION_PETRA_AI)
			continue;
		const difficulty = Math.max(0, Math.min(+data.AIDiff, LEGION_PETRA_RATE.length - 1));
		const rate = LEGION_PETRA_RATE_OVERRIDE[player] ?? LEGION_PETRA_RATE[difficulty];
		const boost = LEGION_PETRA_BOOST[player] || { "time": 1, "strength": 1 };
		const every = ["Unit", "Structure"];
		const modifiers = {
			"ResourceGatherer/BaseSpeed": [{ "affects": every, "multiply": rate }],
			"Trader/GainMultiplier": [{ "affects": every, "multiply": rate }],
			"Cost/BuildTime": [{ "affects": every, "multiply": LEGION_PETRA_TIME[difficulty] * boost.time }]
		};
		if (boost.strength != 1)
		{
			modifiers["Health/Max"] = [{ "affects": every, "multiply": boost.strength }];
			for (const kind of ["Melee", "Ranged"])
				for (const damage of ["Hack", "Pierce", "Crush"])
					modifiers["Attack/" + kind + "/Damage/" + damage] = [{ "affects": every, "multiply": boost.strength }];
		}
		Engine.QueryInterface(SYSTEM_ENTITY, IID_ModifiersManager).AddModifiers("AI Bonus", modifiers, cmpPlayerManager.GetPlayerByID(player));
		warn("LEGION_HANDICAP player=" + player + " ai=" + data.AI + " difficulty=" + difficulty + " rate=" + rate +
			" time=" + LEGION_PETRA_TIME[difficulty] * boost.time + " strength=" + boost.strength);
	}

	if (LEGION_CANDIDATE_BOOST)
	{
		const rate = +LEGION_CANDIDATE_BOOST.rate;
		Engine.QueryInterface(SYSTEM_ENTITY, IID_ModifiersManager).AddModifiers("LEGION boost", {
			"ResourceGatherer/BaseSpeed": [{ "affects": ["Unit", "Structure"], "multiply": rate }],
			"Trader/GainMultiplier": [{ "affects": ["Unit", "Structure"], "multiply": rate }]
		}, cmpPlayerManager.GetPlayerByID(+LEGION_CANDIDATE_BOOST.player));
		warn("LEGION_BOOST player=" + LEGION_CANDIDATE_BOOST.player + " rate=" + rate);
	}

	Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(LEGION_TIME_LIMIT_MS, "LegionTimeLimit", {});
	warn("LEGION_REFEREE ready limit=" + LEGION_TIME_LIMIT_MS);
}
