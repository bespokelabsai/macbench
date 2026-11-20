// LEGION guard. The verifier owns this file. _entry.js imports it before the
// submitted bot, so everything here runs before any submitted code.
//
// 1. The simulation interface (SimEngine) becomes read-only.
// 2. Dynamic code (eval, Function and its async/generator variants) is disabled.
// 3. (Randomness is separated per player by the match runner; see below.)
// 4. Built-in prototypes, globals and the common AI API are frozen, so the
//    submitted AI cannot patch code that Petra also runs.
//
// The guard is one layer. The verifier also re-plays every match the submitted
// AI does not lose using only its logged commands; anything that changed Petra
// any other way makes the match non-reproducible, and it is scored as a loss.

import * as baseAI from "simulation/ai/common-api/baseAI.js";
import * as entity from "simulation/ai/common-api/entity.js";
import * as entityCollection from "simulation/ai/common-api/entitycollection.js";
import * as filters from "simulation/ai/common-api/filters.js";
import * as gameState from "simulation/ai/common-api/gamestate.js";
import * as mapModule from "simulation/ai/common-api/map-module.js";
import * as resources from "simulation/ai/common-api/resources.js";
import * as shared from "simulation/ai/common-api/shared.js";
import * as technology from "simulation/ai/common-api/technology.js";
import * as terrainAnalysis from "simulation/ai/common-api/terrain-analysis.js";
import * as utils from "simulation/ai/common-api/utils.js";

/** Deep copy of plain data; functions are dropped. */
export function cloneData(value, seen = new Map())
{
	if (typeof value === "function")
		return undefined;
	if (value === null || typeof value !== "object")
		return value;
	if (seen.has(value))
		return seen.get(value);
	if (ArrayBuffer.isView(value))
	{
		const copy = value.slice();
		seen.set(value, copy);
		return copy;
	}
	// The engine's state comes from another realm, where `instanceof Map` is
	// false; the type tag works across realms.
	const tag = Object.prototype.toString.call(value);
	if (tag === "[object Map]")
	{
		const copy = new Map();
		seen.set(value, copy);
		value.forEach((v, k) => copy.set(cloneData(k, seen), cloneData(v, seen)));
		return copy;
	}
	if (tag === "[object Set]")
	{
		const copy = new Set();
		seen.set(value, copy);
		value.forEach(v => copy.add(cloneData(v, seen)));
		return copy;
	}
	const copy = Array.isArray(value) ? [] : {};
	seen.set(value, copy);
	for (const key of Object.keys(value))
		copy[key] = cloneData(value[key], seen);
	return copy;
}

// --- 1. read-only simulation interface ---------------------------------------

const realSimEngine = SimEngine;
const readMethod = /^(Get|Is|Has|Can|Calculate|Count|Find)/;
const blocked = new Set();

function reportBlocked(name)
{
	if (blocked.has(name))
		return;
	blocked.add(name);
	warn("LEGION_GUARD_BLOCKED " + name);
}

function readOnlyComponent(component)
{
	return new Proxy(component, {
		"get": (target, key) => {
			const value = target[key];
			if (typeof value !== "function")
				return cloneData(value);
			if (typeof key === "string" && readMethod.test(key))
				return (...args) => cloneData(value.apply(target, args.map(arg => cloneData(arg))));
			return () => {
				reportBlocked(String(key));
				return undefined;
			};
		},
		"set": (target, key) => {
			reportBlocked("set " + String(key));
			return true;
		},
		"defineProperty": () => true,
		"deleteProperty": () => true,
		"setPrototypeOf": () => false,
		"getPrototypeOf": () => null,
		"getOwnPropertyDescriptor": () => undefined
	});
}

Object.defineProperty(globalThis, "SimEngine", {
	"value": Object.freeze({
		"QueryInterface": (ent, iid) => {
			const component = realSimEngine.QueryInterface(+ent, +iid);
			return component ? readOnlyComponent(component) : component;
		}
	}),
	"writable": false,
	"configurable": false
});

// --- 2. no dynamic code ------------------------------------------------------

const dynamicCodeDisabled = function()
{
	throw new TypeError("LEGION: dynamic code evaluation is disabled");
};
for (const fn of [function() {}, function*() {}, async function() {}, async function*() {}])
	Object.defineProperty(Object.getPrototypeOf(fn), "constructor", {
		"value": dynamicCodeDisabled,
		"writable": false,
		"configurable": false
	});
for (const name of ["eval", "Function"])
	Object.defineProperty(globalThis, name, { "value": dynamicCodeDisabled, "writable": false, "configurable": false });

// --- 3. randomness -------------------------------------------------------------
// The engine locks Math.random, so it cannot be split per player here. Instead
// the match runner patches Petra (and common-api's one random call) to draw from
// simulation/ai/legion_rng/rng.js, which keeps one stream per player. Math.random
// is then used by nobody but the submitted AI.

// --- 4. freeze shared code -----------------------------------------------------

/**
 * Freeze an object without breaking subclasses. A plain freeze would make
 * `Sub.prototype.OnUpdate = ...` throw, because OnUpdate is inherited and
 * read-only; here each property becomes an accessor whose setter defines the
 * property on the inheriting object instead (the "override mistake" fix).
 */
function tameAndFreeze(object)
{
	for (const key of Reflect.ownKeys(object))
	{
		const descriptor = Object.getOwnPropertyDescriptor(object, key);
		if (!("value" in descriptor) || !descriptor.configurable)
			continue;
		const value = descriptor.value;
		Object.defineProperty(object, key, {
			"get": function() { return value; },
			"set": function(newValue)
			{
				if (this === object)
					throw new TypeError("LEGION: " + String(key) + " is frozen");
				Object.defineProperty(this, key, { "value": newValue, "writable": true, "enumerable": true, "configurable": true });
			},
			"enumerable": descriptor.enumerable,
			"configurable": false
		});
	}
	Object.freeze(object);
}

for (const namespace of [baseAI, entity, entityCollection, filters, gameState, mapModule, resources,
	shared, technology, terrainAnalysis, utils])
	for (const key of Object.keys(namespace))
	{
		const value = namespace[key];
		if (typeof value === "function" && value.prototype)
			tameAndFreeze(value.prototype);
		if (value && (typeof value === "function" || typeof value === "object"))
			tameAndFreeze(value);
	}

const typedArray = Object.getPrototypeOf(Uint8Array);
for (const intrinsic of [Array, Array.prototype, Function.prototype, String, String.prototype, Number,
	Number.prototype, Boolean, Boolean.prototype, Symbol, Symbol.prototype, Map, Map.prototype, Set,
	Set.prototype, WeakMap, WeakMap.prototype, WeakSet, WeakSet.prototype, Date, Date.prototype, RegExp,
	RegExp.prototype, JSON, Math, Reflect, Promise, Promise.prototype, ArrayBuffer, ArrayBuffer.prototype,
	typedArray, typedArray.prototype, Uint8Array, Int8Array, Uint16Array, Int16Array, Uint32Array,
	Int32Array, Float32Array, Float64Array, Uint8ClampedArray, Object])
	tameAndFreeze(intrinsic);

// Every existing global except PlayerID (set by each AI on its turn) becomes read-only.
for (const name of Object.getOwnPropertyNames(globalThis))
{
	if (name === "PlayerID")
		continue;
	const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
	if (descriptor.configurable && "value" in descriptor)
		Object.defineProperty(globalThis, name, { "value": descriptor.value, "writable": false, "configurable": false });
}

warn("LEGION_GUARD installed");
