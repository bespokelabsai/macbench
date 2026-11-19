// LEGION referee: autostart games enable cheats, so cheat commands are blocked
// for every player and logged instead of applied.
function Cheat(input)
{
	warn("LEGION_CHEAT_BLOCKED player=" + input.player + " action=" + input.action);
}

Engine.RegisterGlobal("Cheat", Cheat);
