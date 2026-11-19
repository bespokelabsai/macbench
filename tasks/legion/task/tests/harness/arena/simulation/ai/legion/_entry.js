// LEGION entry point. The verifier owns this file and places the submitted
// bot under simulation/ai/legion/bot/. The guard is imported first, so it is
// evaluated before any submitted module.
import { cloneData } from "simulation/ai/legion/guard.js";
import { SharedScript } from "simulation/ai/common-api/shared.js";
import { LegionBot } from "simulation/ai/legion/bot/main.js";

/**
 * Wraps the submitted LegionBot. The bot gets its own SharedScript built from
 * a private copy of each turn's state, so it never holds references to the
 * engine's shared objects or to another AI.
 */
export function LegionEntry(settings)
{
	this.player = settings.player;
	this.shared = undefined;
	this.inner = this.asPlayer(() => new LegionBot(cloneData(settings)));
	// The match runner scores a match as a loss unless this line appears.
	warn("LEGION_BOT_READY");
}

LegionEntry.prototype.asPlayer = function(fn)
{
	const previous = PlayerID;
	PlayerID = this.player;
	try
	{
		return fn();
	}
	finally
	{
		PlayerID = previous;
	}
};

LegionEntry.prototype.Init = function(state, playerID, sharedAI)
{
	const privateState = cloneData(state);
	this.shared = new SharedScript({
		"players": cloneData(sharedAI._players),
		"templates": cloneData(sharedAI._templates)
	});
	this.asPlayer(() => {
		this.shared.init(privateState);
		this.inner.Init(privateState, playerID, this.shared);
	});
};

LegionEntry.prototype.HandleMessage = function(state, playerID, sharedAI)
{
	const privateState = cloneData(state);
	this.asPlayer(() => {
		this.shared.onUpdate(privateState);
		this.inner.HandleMessage(privateState, playerID, this.shared);
	});
};

LegionEntry.prototype.Serialize = function()
{
	return {};
};

LegionEntry.prototype.Deserialize = function()
{
};
