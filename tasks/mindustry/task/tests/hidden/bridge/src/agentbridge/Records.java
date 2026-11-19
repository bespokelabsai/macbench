// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.math.geom.Point2;
import arc.math.geom.Vec2;
import arc.util.serialization.JsonValue;
import mindustry.Vars;
import mindustry.ai.types.CommandAI;
import mindustry.content.Planets;
import mindustry.ctype.*;
import mindustry.entities.units.BuildPlan;
import mindustry.world.Build;
import mindustry.content.Blocks;
import mindustry.world.blocks.ConstructBlock.ConstructBuild;
import mindustry.game.Team;
import mindustry.gen.*;
import mindustry.logic.LAccess;
import mindustry.type.*;
import mindustry.world.*;
import mindustry.world.blocks.ControlBlock;
import mindustry.world.blocks.environment.Floor;
import mindustry.world.blocks.environment.OverlayFloor;
import java.util.*;
import static agentbridge.AgentBridge.*;

class Records {
    final AgentBridge bridge;
    static final Object MISSING=new Object();
    /**
     * Sight decisions, one per collection. Rows are explicit field lists, never a dump of the engine's sensors: native sensors
     * include turret aim, unit targets, mining and build coordinates and pings, all of which can name things out of sight.
     *  collections, ops, blocks, unit_types, items, liquids: static catalogs, no world data.
     *  match: size, wave number and countdown, world index, sight radius, own core. Never spawns, wave composition or seeds.
     *  players: self in full; other seats by name only.
     *  teams: own team only; its rebuild plans are its own buildings.
     *  units, bullets: only within sight, own team included.
     *  buildings: own team anywhere, other teams within sight.
     *  tiles: only within sight.
     *  production (removed): per-building output history was not needed by the game and duplicates the graded counters.
     */
    static final List<String> NAMES=List.of("collections","match","players","teams","units","buildings","tiles","bullets","blocks","unit_types","items","liquids","ops");
    Records(AgentBridge bridge){this.bridge=bridge;}
    static void only(JsonValue a,String... names){
        require(a!=null&&a.isObject(),"Arguments must be an object");
        Set<String> allowed=new HashSet<>(Arrays.asList(names));
        Set<String> seen=new HashSet<>();
        for(JsonValue child:a){require(allowed.contains(child.name),"Unknown field "+child.name);require(seen.add(child.name),"Duplicate field "+child.name);}
    }

    static String show(JsonValue v){return json(plain(v));}
    static boolean integer(JsonValue v){return v!=null&&v.isNumber()&&Double.isFinite(v.asDouble())&&v.asDouble()==v.asLong()&&v.asDouble()>=Integer.MIN_VALUE&&v.asDouble()<=Integer.MAX_VALUE;}
    static boolean number(JsonValue v){return v!=null&&v.isNumber()&&Double.isFinite(v.asDouble());}
    static void field(JsonValue a,String name,String type,boolean required){
        JsonValue v=a.get(name);if(v==null){require(!required,"Missing "+name);return;}
        boolean valid=switch(type){case "string"->v.isString();case "boolean"->v.isBoolean();case "object"->v.isObject();case "array"->v.isArray();case "int"->integer(v);case "number"->number(v);case "json"->true;default->throw new IllegalStateException(type);};
        require(valid,name+" must be "+type);
    }
    static Object plain(JsonValue v){
        if(v==null||v.isNull())return null;
        if(v.isString())return v.asString();if(v.isBoolean())return v.asBoolean();
        if(v.isNumber()){require(number(v),"JSON numbers must be finite");return v.isLong()?(Object)v.asLong():v.asDouble();}
        if(v.isArray()){ArrayList<Object> out=new ArrayList<>();for(JsonValue child:v)out.add(plain(child));return out;}
        LinkedHashMap<String,Object> out=new LinkedHashMap<>();for(JsonValue child:v)out.put(child.name,plain(child));return out;
    }
    static List<String> fields(JsonValue value){
        if(value==null)return null;
        require(value.isArray(),"fields must be an array");ArrayList<String> out=new ArrayList<>();
        for(JsonValue v:value){require(v.isString()&&v.asString().matches("[^.]+(\\.[^.]+)*"),"fields must contain dotted paths");out.add(v.asString());}return out;
    }
    static boolean wholeNumber(JsonValue value){
        return value!=null&&value.isNumber()&&Double.isFinite(value.asDouble())&&value.asDouble()==Math.rint(value.asDouble());
    }
    static double limit(JsonValue a){
        if(!a.has("limit"))return 128;
        JsonValue value=a.get("limit");
        require(wholeNumber(value)&&value.asDouble()>=1,"limit must be a positive integer");
        return value.asDouble();
    }
    record Predicate(String[] path,String op,Object expected){}
    static final List<String> FILTER_OPS=List.of("eq","ne","in","not_in","lt","le","gt","ge","contains");

    static List<Predicate> filter(JsonValue value,String where){
        if(value==null)return List.of();require(value.isArray(),where+": expected an array of [path, op, value]");
        ArrayList<Predicate> out=new ArrayList<>();int index=0;
        for(JsonValue predicate:value){
            String at=where+"["+(index++)+"]";
            require(predicate.isArray()&&predicate.size==3,at+": expected [path, op, value]");
            JsonValue path=predicate.get(0),op=predicate.get(1),v=predicate.get(2);
            require(path.isString()&&path.asString().matches("[^.]+(\\.[^.]+)*"),at+": the path must be field names joined by dots");
            require(op.isString()&&FILTER_OPS.contains(op.asString()),at+": op "+show(op)+" is not one of "+list(FILTER_OPS));
            if(op.asString().equals("in")||op.asString().equals("not_in"))require(v.isArray(),at+": "+op.asString()+" needs an array value");
            if(List.of("lt","le","gt","ge").contains(op.asString()))require(number(v),at+": "+op.asString()+" needs a finite number");
            out.add(new Predicate(path.asString().split("\\."),op.asString(),plain(v)));
        }return out;
    }

    static final Map<String,Map<String,String>> NAMED=Map.of(
        "units",Map.of("type","unit type","team","team","stack.item","item"),
        "buildings",Map.of("type","block","team","team"),
        "bullets",Map.of("team","team"),
        "tiles",Map.of("floor","floor","ore","ore","block","block","team","team"),
        "blocks",Map.of("name","block"),
        "unit_types",Map.of("name","unit type"),
        "items",Map.of("name","item"),
        "liquids",Map.of("name","liquid"));
    static final Map<String,Set<String>> KNOWN=new HashMap<>();
    static Set<String> known(String kind){
        return KNOWN.computeIfAbsent(kind,k->{
            LinkedHashSet<String> names=new LinkedHashSet<>();
            switch(k){
                case "team"->{for(Team t:Team.all)names.add(t.name);}
                case "unit type"->{for(UnitType u:Vars.content.units())names.add(u.name);}
                case "item"->{for(Item i:Vars.content.items())names.add(i.name);}
                case "liquid"->{for(Liquid l:Vars.content.liquids())names.add(l.name);}
                case "floor"->{for(Block b:Vars.content.blocks())if(b instanceof Floor&&!(b instanceof OverlayFloor))names.add(b.name);}
                case "ore"->{for(Block b:Vars.content.blocks())if(b instanceof OverlayFloor)names.add(b.name);}
                default->{for(Block b:Vars.content.blocks())names.add(b.name);}
            }
            return names;
        });
    }
    static void knownNames(String of,List<Predicate> predicates){
        Map<String,String> named=NAMED.getOrDefault(of,Map.of());
        for(int i=0;i<predicates.size();i++){
            Predicate p=predicates.get(i);String kind=named.get(String.join(".",p.path()));
            if(kind==null||!List.of("eq","ne","in","not_in").contains(p.op()))continue;
            for(Object value:p.expected() instanceof List<?> values?values:Collections.singletonList(p.expected())){
                if(!(value instanceof String name)||known(kind).contains(name))continue;
                List<String> near=known(kind).stream().filter(other->other.contains(name)).toList();
                require(false,"filter["+i+"]: no "+kind+" is named \""+name+"\""+(near.isEmpty()?"":"; the "+kind+" names containing it are "+list(near)));
            }
        }
    }
    static Object path(Object object,String[] path){
        for(String part:path){if(!(object instanceof Map<?,?> map)||!map.containsKey(part))return MISSING;object=map.get(part);}return object;
    }
    static boolean equal(Object a,Object b){
        if(a instanceof Number x&&b instanceof Number y)return x.doubleValue()==y.doubleValue();
        if(a instanceof List<?> x&&b instanceof List<?> y){if(x.size()!=y.size())return false;for(int i=0;i<x.size();i++)if(!equal(x.get(i),y.get(i)))return false;return true;}
        if(a instanceof Map<?,?> x&&b instanceof Map<?,?> y){if(!x.keySet().equals(y.keySet()))return false;for(Object k:x.keySet())if(!equal(x.get(k),y.get(k)))return false;return true;}
        return Objects.equals(a,b);
    }
    static boolean matches(Map<String,Object> row,List<Predicate> predicates){
        for(Predicate p:predicates){Object v=path(row,p.path);if(v==MISSING)return false;
            boolean match=switch(p.op){
                case "eq"->equal(v,p.expected);
                case "ne"->!equal(v,p.expected);
                case "in"->((List<?>)p.expected).stream().anyMatch(e->equal(v,e));
                case "not_in"->((List<?>)p.expected).stream().noneMatch(e->equal(v,e));
                case "contains"->v instanceof List<?> list?list.stream().anyMatch(e->equal(e,p.expected)):v instanceof String s&&p.expected instanceof String t&&s.contains(t);
                default->{if(!(v instanceof Number n))yield false;double x=n.doubleValue(),y=((Number)p.expected).doubleValue();yield switch(p.op){case "lt"->x<y;case "le"->x<=y;case "gt"->x>y;case "ge"->x>=y;default->false;};}
            };if(!match)return false;
        }return true;
    }
    static Map<String,Object> project(Map<String,Object> row,List<String> fields){
        if(fields==null)return row;
        LinkedHashMap<String,Object> out=new LinkedHashMap<>();
        for(String field:fields){String[] parts=field.split("\\.");Object value=path(row,parts);if(value==MISSING)continue;
            Map<String,Object> target=out;
            for(int i=0;i<parts.length-1;i++){
                Object child=target.get(parts[i]);
                if(!(child instanceof Map<?,?>)){child=new LinkedHashMap<String,Object>();target.put(parts[i],child);}

                @SuppressWarnings("unchecked") Map<String,Object> copy=new LinkedHashMap<>((Map<String,Object>)child);
                target.put(parts[i],copy);target=copy;
            }target.put(parts[parts.length-1],value);
        }return out;
    }
    static boolean inArea(double px,double py,double x,double y,double w,double h){
        return px>=x&&px<x+w&&py>=y&&py<y+h;
    }
    Object query(AgentBridge.Seat seat,JsonValue a){
        only(a,"seat","of","filter","area","fields","limit","after");field(a,"of","string",true);
        String of=a.getString("of");require(NAMES.contains(of),"of: \""+of+"\" is not one of "+list(NAMES));
        List<Predicate> filter=filter(a.get("filter"),"filter");knownNames(of,filter);List<String> fields=fields(a.get("fields"));double limit=limit(a);
        if(a.has("after"))require(wholeNumber(a.get("after")),"after must be an integer");
        double after=a.has("after")?a.getDouble("after"):-Double.MAX_VALUE;
        JsonValue area=a.get("area");double x=0,y=0,w=0,h=0;
        if(area!=null){only(area,"x","y","w","h");for(String k:List.of("x","y","w","h"))field(area,k,"number",true);x=area.getDouble("x");y=area.getDouble("y");w=area.getDouble("w");h=area.getDouble("h");require(w>=0&&h>=0,"Area dimensions must be nonnegative");}
        Team team=seat.player.team();
        // Candidates hold only what this seat may see, so filter, area and paging below can never touch an unseen row.
        ArrayList<Object> candidates=new ArrayList<>();
        switch(of){
            case "collections"->candidates.addAll(NAMES);
            case "match"->candidates.add("match");
            case "players"->candidates.addAll(bridge.seats.values());
            case "teams"->candidates.add(team);
            case "units"->{for(Unit u:Groups.unit)if(u.isValid()&&bridge.sees(seat,u.x/8,u.y/8))candidates.add(u);}
            case "buildings"->{for(Team t:Team.all)for(Building b:t.data().buildings)if(t==team||bridge.sees(seat,b.x/8,b.y/8))candidates.add(b);}
            case "tiles"->{
                var center=bridge.center(seat);
                if(center!=null){
                    int r=bridge.profile.sightRadius(),cx=(int)center.x,cy=(int)center.y;
                    int x0=Math.max(0,cx-r-1),x1=Math.min(Vars.world.width()-1,cx+r+1),y0=Math.max(0,cy-r-1),y1=Math.min(Vars.world.height()-1,cy+r+1);
                    // Sight can cover the whole map, so a small area must not walk it all. One tile of slack, as in outside().
                    if(area!=null){x0=Math.max(x0,(int)Math.floor(x)-1);x1=Math.min(x1,(int)Math.ceil(x+w)+1);y0=Math.max(y0,(int)Math.floor(y)-1);y1=Math.min(y1,(int)Math.ceil(y+h)+1);}
                    for(int ty=y0;ty<=y1;ty++)
                        for(int tx=x0;tx<=x1;tx++)
                            if(bridge.sees(seat,tx,ty))candidates.add(Vars.world.tile(tx,ty));
                }
            }
            case "bullets"->{for(Bullet b:Groups.bullet)if(bridge.sees(seat,b.x/8,b.y/8))candidates.add(b);}
            case "blocks"->{for(Block b:Vars.content.blocks())if(b.isPlaceable()&&b.environmentBuildable())candidates.add(b);}
            case "unit_types"->{for(UnitType u:Vars.content.units())if(!u.isHidden()&&u.isOnPlanet(Planets.serpulo))candidates.add(u);}
            case "items"->{for(Item i:Vars.content.items())if(i.isOnPlanet(Planets.serpulo))candidates.add(i);}
            case "liquids"->{for(Liquid l:Vars.content.liquids())if(l.isOnPlanet(Planets.serpulo))candidates.add(l);}
            case "ops"->candidates.addAll(bridge.inputs.table.keySet());
        }
        candidates.sort(Comparator.comparingInt(candidate->candidateKey(of,candidate)));
        ArrayList<Object> rows=new ArrayList<>();Integer last=null,next=null;int scanned=0;
        boolean catalog=CATALOGS.contains(of);
        for(Object candidate:candidates){
            int key=candidateKey(of,candidate);
            if(key<=after)continue;
            // Position first: a row is only built for what the area can keep. Rows cost work on the game thread, so the
            // host charges the seat for `scanned`. Catalogs never change, so they are built once.
            if(area!=null&&outside(candidate,x,y,w,h))continue;
            Map<String,Object> row;
            if(catalog)row=rowCache.computeIfAbsent(of+":"+key,k->{Map<String,Object> built=row(of,candidate,seat);built.put("id",key);return built;});
            else{row=row(of,candidate,seat);row.put("id",key);scanned++;}
            if(area!=null&&!(row.get("x") instanceof Number rx&&row.get("y") instanceof Number ry&&inArea(rx.doubleValue(),ry.doubleValue(),x,y,w,h)))continue;
            if(!matches(row,filter))continue;
            if(rows.size()==limit){next=last;break;}
            rows.add(project(row,fields));last=key;
        }
        return obj("rows",rows,"next",next,"tick",Vars.state.updateId,"at",bridge.log.head(seat.name),"scanned",scanned);
    }
    static final Set<String> CATALOGS=Set.of("collections","blocks","unit_types","items","liquids","ops");
    final Map<String,Map<String,Object>> rowCache=new HashMap<>();
    /** True when the candidate's row cannot fall in the area. One tile of slack: the row's own coordinates decide the rest. */
    static boolean outside(Object c,double x,double y,double w,double h){
        double px,py;
        if(c instanceof Tile t){px=t.x;py=t.y;}
        else if(c instanceof Building b){px=b.tileX();py=b.tileY();}
        else if(c instanceof Unit u){px=u.x/8;py=u.y/8;}
        else if(c instanceof Bullet b){px=b.x/8;py=b.y/8;}
        else return false;
        return !inArea(px,py,x-1,y-1,w+2,h+2);
    }
    int candidateKey(String of,Object candidate){
        if(candidate instanceof Tile t)return t.y*Vars.world.width()+t.x;
        if(candidate instanceof AgentBridge.Seat s)return s.index;
        if(candidate instanceof Entityc entity)return entity.id();
        if(candidate instanceof Content content)return content.id;
        if(candidate instanceof Team team)return team.id;
        return switch(of){
            case "collections"->NAMES.indexOf(candidate);
            case "match"->0;
            case "ops"->bridge.inputs.table.get(candidate).id();
            default->throw new IllegalStateException("No key for "+of);
        };
    }
    Map<String,Object> row(String of,Object candidate,AgentBridge.Seat seat){
        Team team=seat.player.team();
        return switch(of){
            case "collections"->collection((String)candidate);
            case "match"->{
                Building core=team.core();
                yield obj("name","match","tick",Vars.state.updateId,"world_index",bridge.worldIndex,"width",Vars.world.width(),"height",Vars.world.height(),
                    "sight_radius_tiles",bridge.profile.sightRadius(),"wave",Vars.state.wave,"next_wave_in_seconds",Vars.state.wavetime/60,
                    "core",core==null?null:obj("id",core.id,"x",core.tileX(),"y",core.tileY()));
            }
            case "players"->{
                AgentBridge.Seat s=(AgentBridge.Seat)candidate;Player p=s.player;
                yield s==seat?obj("seat",s.name,"team",p.team().name,"unit_id",p.dead()?null:p.unit().id,"dead",p.dead()):obj("seat",s.name);
            }
            case "teams"->{
                Team t=(Team)candidate;ArrayList<Object> cores=new ArrayList<>();for(var core:t.cores())cores.add(core.id);
                ArrayList<Object> plans=new ArrayList<>();
                for(var plan:t.data().plans)if(!plan.block.privileged)
                    plans.add(obj("x",plan.x,"y",plan.y,"block",plan.block.name,"rotation",plan.rotation,"config",config(plan.config,plan.x,plan.y)));
                yield obj("name",t.name,"cores",cores,"plans",plans);
            }
            case "units"->unit((Unit)candidate,seat);
            case "buildings"->building((Building)candidate,team,seat);
            case "bullets"->{Bullet b=(Bullet)candidate;yield obj("id",b.id,"kind","bullet","type",b.type.id,"team",b.team.name,"x",b.x/8,"y",b.y/8,"rotation",b.rotation(),"damage",b.damage);}
            case "tiles"->{
                Tile t=(Tile)candidate;Building b=t.build;
                yield obj("x",t.x,"y",t.y,"floor",t.floor().name,"ore",t.overlay()==Blocks.air?null:t.overlay().name,"block",t.block().name,"team",t.team().name,
                    "building",b!=null&&(b.team==team||bridge.sees(seat,b.x/8,b.y/8))?b.id:null);
            }
            case "blocks"->block((Block)candidate);
            case "unit_types"->unitType((UnitType)candidate);
            case "items"->{Item i=(Item)candidate;yield obj("name",i.name,"description",CatalogText.description(i),"hardness",i.hardness,"cost",i.cost,"flammability",i.flammability,"explosiveness",i.explosiveness,"radioactivity",i.radioactivity,"charge",i.charge);}
            case "liquids"->{Liquid l=(Liquid)candidate;yield obj("name",l.name,"description",CatalogText.description(l),"temperature",l.temperature,"heat_capacity",l.heatCapacity,"viscosity",l.viscosity,"flammability",l.flammability,"explosiveness",l.explosiveness);}
            case "ops"->bridge.inputs.table.get(candidate).record();
            default->throw new IllegalArgumentException("Unknown collection "+of);
        };
    }
    Map<String,Object> collection(String name){
        List<String> fields=switch(name){
            case "collections"->List.of("id","name","fields");
            case "match"->List.of("id","name","tick","world_index","width","height","sight_radius_tiles","wave","next_wave_in_seconds","core");
            case "players"->List.of("id","seat","team","unit_id","dead");
            case "teams"->List.of("id","name","cores","plans");
            case "tiles"->List.of("id","x","y","floor","ore","block","team","building");
            case "ops"->List.of("id","name","fields","description");
            case "blocks"->List.of("id","name","localized_name","description","size","footprint_offset","health","category","cost","build_time","config_types","mechanics");
            case "unit_types"->List.of("id","name","description","speed","health","armor","flying","build_speed","build_range_tiles","mine_tier","mine_range_tiles","mine_floor","mine_walls","item_capacity","weapons","commands");
            case "items"->List.of("id","name","description","hardness","cost","flammability","explosiveness","radioactivity","charge");
            case "liquids"->List.of("id","name","description","temperature","heat_capacity","viscosity","flammability","explosiveness");
            case "units"->List.of("id","kind","type","team","x","y","rotation","health","max_health","shield","armor","flying","stack","controlled","plans","command","stance","building_active","mining");
            case "buildings"->List.of("id","kind","type","team","x","y","size","rotation","health","max_health","footprint","controlled","enabled","efficiency","progress","first_item","items","liquids","ammo","ammo_capacity","power_status","power_net_in","power_net_out","power_links","power_links_frame","config");
            default->List.of("id","kind","type","team","x","y","rotation","damage");
        };
        return obj("name",name,"fields",fields);
    }
    static Object ref(Object value){
        if(value==null)return null;
        if(value instanceof Building b)return obj("id",b.id,"type",b.block.name,"team",b.team.name,"x",b.tileX(),"y",b.tileY());
        if(value instanceof Unit u)return obj("id",u.id,"type",u.type.name,"team",u.team.name,"x",u.x/8,"y",u.y/8);
        if(value instanceof Tile t)return obj("id",t.y*Vars.world.width()+t.x,"type",t.block().name,"team",t.team().name,"x",t.x,"y",t.y);
        if(value instanceof Vec2 p)return obj("x",p.x/8,"y",p.y/8);
        throw new IllegalArgumentException("Unsupported reference "+value.getClass().getSimpleName());
    }

    static LinkedHashMap<String,Object> items(Building b){
        LinkedHashMap<String,Object> items=new LinkedHashMap<>();
        if(b.items!=null)for(Item item:Vars.content.items()){double n=b.sense(item);if(n!=0&&Double.isFinite(n))items.put(item.name,n);}
        return items;
    }
    static Object stack(Unit u){return obj("item",u.stack.item==null?null:u.stack.item.name,"amount",u.stack.amount);}
    static Map<String,Object> queuedPlan(BuildPlan plan){
        var out=obj("x",plan.x,"y",plan.y,"breaking",plan.breaking,"block",plan.block==null?null:plan.block.name,"rotation",plan.rotation);
        if(plan.config!=null)out.put("config",config(plan.config,plan.x,plan.y));
        return out;
    }

    static final java.lang.reflect.Field CONSTRUCTION_ITEMS_LEFT=constructionItemsLeftField();
    static java.lang.reflect.Field constructionItemsLeftField(){
        try{var field=ConstructBuild.class.getDeclaredField("itemsLeft");field.setAccessible(true);return field;}
        catch(ReflectiveOperationException e){throw new IllegalStateException("Native construction resource layout changed",e);}
    }
    static int[] constructionItemsLeft(ConstructBuild build){
        try{return (int[])CONSTRUCTION_ITEMS_LEFT.get(build);}
        catch(IllegalAccessException e){throw new IllegalStateException("Cannot inspect native construction resources",e);}
    }
    Map<String,Object> plan(Unit unit,BuildPlan plan){
        var out=queuedPlan(plan);if(plan.config==null)out.put("config",null);
        Tile tile=plan.tile();
        ConstructBuild build=tile!=null&&tile.build instanceof ConstructBuild cb?cb:null;
        boolean matching=build!=null&&(tile.team()==unit.team||tile.team()==Team.derelict)
            &&(plan.breaking||(build.current==plan.block&&build.tile==tile));
        boolean complete=tile!=null&&(plan.breaking?tile.block()==Blocks.air:
            tile.team()!=Team.derelict&&((tile.block()==plan.block&&(!plan.block.rotate||tile.build!=null&&tile.build.rotation==plan.rotation))
            ||plan.block.isOverlay()&&tile.overlay()==plan.block||plan.block.isFloor()&&tile.floor()==plan.block));
        Float progress=null;
        if(matching)progress=Math.max(0f,Math.min(1f,build.progress));
        else if(complete)progress=plan.breaking?0f:1f;
        else if(!plan.initialized)progress=plan.breaking?1f:0f;
        var reasons=new ArrayList<String>();
        var core=unit.core();
        boolean infinite=Vars.state.rules.infiniteResources||unit.team.rules().infiniteResources;
        if(!complete){
            if(!unit.updateBuilding)reasons.add("building_disabled");
            if(!unit.canBuild()||Vars.state.rules.buildSpeed(unit.team)<=0)reasons.add("build_speed_zero");
            if(tile==null)reasons.add("invalid_target");
            else if(!Vars.state.rules.infiniteResources&&!unit.within(tile,unit.type.buildRange))reasons.add("out_of_range");
            if(core==null&&!infinite)reasons.add("no_core");
            boolean allowed=plan.block!=null&&(Vars.state.isEditor()||(Vars.state.rules.waves&&unit.team==Vars.state.rules.waveTeam&&plan.block.isVisible())
                ||plan.block.unlockedNowHost()&&plan.block.environmentBuildable()&&plan.block.isPlaceable());
            if(!plan.breaking&&!allowed)reasons.add("block_unavailable");
            if(build!=null&&!matching)reasons.add("invalid_target");
            if(tile!=null&&build==null){
                if(plan.initialized)reasons.add("invalid_target");
                else if(plan.breaking){if(!Build.validBreak(unit.team,plan.x,plan.y))reasons.add("invalid_target");}
                else{
                    if(!Build.validPlaceIgnoreUnits(plan.block,unit.team,plan.x,plan.y,plan.rotation,true,true))reasons.add("invalid_placement");
                    if(!Build.checkNoUnitOverlap(plan.block,plan.x,plan.y))reasons.add("unit_obstruction");
                }
            }
            if(!plan.breaking&&matching&&build.progress>=1f&&plan.block.isOverPlacementLimit(unit.team))reasons.add("placement_limit");
            if(!plan.breaking&&!infinite&&core!=null&&plan.block!=null){
                boolean missing=false;
                if(matching){
                    int[] left=constructionItemsLeft(build);
                    if(left==null)reasons.add("construction_uninitialized");
                    else{
                        if(left.length!=build.current.requirements.length)throw new IllegalStateException("Native construction resource count changed");
                        for(int i=0;i<left.length;i++){
                            var req=build.current.requirements[i];
                            if(left[i]>0&&(build.progress>=1f?core.items.get(req.item)<left[i]:Math.round(Vars.state.rules.buildCostMultiplier*req.amount)>0&&core.items.get(req.item)==0))missing=true;
                        }
                    }
                }else if(build==null&&!plan.isRotation(unit.team)&&!(plan.isDerelictRepair()&&plan.block.allowDerelictRepair)){
                    for(var req:plan.block.requirements)if(!core.items.has(req.item,Math.min(Math.round(req.amount*Vars.state.rules.buildCostMultiplier),1)))missing=true;
                }
                if(missing)reasons.add("missing_resources");
            }
        }
        out.put("progress",progress);out.put("progress_source",matching||complete?"world":!plan.initialized?"initial":"unavailable");
        out.put("target_complete",complete);out.put("stuck",plan.stuck);
        out.put("state",!reasons.isEmpty()?"blocked":!complete&&unit.buildPlan()==plan&&matching?"building":"queued");
        out.put("blocked_reasons",reasons);
        return out;
    }
    Map<String,Object> unit(Unit u,AgentBridge.Seat seat){
        var out=obj("id",u.id,"kind","unit","type",u.type.name,"team",u.team.name,"x",u.x/8,"y",u.y/8,"rotation",u.rotation,
            "health",u.health,"max_health",u.maxHealth,"shield",u.shield,"armor",u.armor,"flying",u.isFlying(),"stack",stack(u),"controlled",controlledBy(u));
        if(u.team==seat.player.team()){
            CommandAI ai=u.controller() instanceof CommandAI c?c:null;
            ArrayList<Object> stances=new ArrayList<>();
            if(ai!=null)for(var stance:Vars.content.unitStances())if(ai.hasStance(stance))stances.add(stance.name);
            out.putAll(obj("plans",plans(u,seat),"command",ai==null?null:ai.currentCommand().name,"stance",stances));
        }
        if(u==seat.player.unit()&&!seat.player.dead())out.putAll(obj("building_active",u.updateBuilding,"mining",u.mineTile==null?null:obj("x",(int)u.mineTile.x,"y",(int)u.mineTile.y)));
        return out;
    }
    /** Queued plans: own unit's plans are always listed, without diagnostics when out of sight; other units' only when in sight. */
    List<Object> plans(Unit u,AgentBridge.Seat seat){
        ArrayList<Object> out=new ArrayList<>();boolean self=!seat.player.dead()&&u==seat.player.unit();
        for(BuildPlan plan:u.plans){
            if(bridge.sees(seat,plan.x,plan.y))out.add(plan(u,plan));
            else if(self)out.add(queuedPlan(plan));
        }
        return out;
    }
    static String controlledBy(Unit u){
        var controller=u.controller();
        return controller instanceof Player?"player":controller instanceof CommandAI?"command":"ai";
    }
    Map<String,Object> building(Building b,Team viewer,AgentBridge.Seat seat){
        int x=b.tileX()+b.block.sizeOffset,y=b.tileY()+b.block.sizeOffset;
        var out=obj("id",b.id,"kind","building","type",b.block.name,"team",b.team.name,"x",b.tileX(),"y",b.tileY(),"size",b.block.size,"rotation",b.rotation,
            "health",b.health,"max_health",b.maxHealth,"footprint",obj("x0",x,"y0",y,"x1",x+b.block.size,"y1",y+b.block.size));
        if(b.team!=viewer)return out;
        LinkedHashMap<String,Object> liquids=new LinkedHashMap<>();
        if(b.liquids!=null)for(Liquid liquid:Vars.content.liquids()){double n=b.sense(liquid);if(n!=0&&Double.isFinite(n))liquids.put(liquid.name,n);}
        ArrayList<Object> links=new ArrayList<>();if(b.power!=null)for(int i=0;i<b.power.links.size;i++){int p=b.power.links.get(i);links.add(obj("x",Point2.x(p),"y",Point2.y(p)));}
        Object first=b.senseObject(LAccess.firstItem);
        out.putAll(obj("controlled",b instanceof ControlBlock control&&control.isControlled()?"player":"none","enabled",b.enabled,"efficiency",b.efficiency,"progress",b.sense(LAccess.progress),
            "first_item",first instanceof Item item?item.name:null,"items",items(b),"liquids",liquids,"ammo",b.sense(LAccess.ammo),"ammo_capacity",b.sense(LAccess.ammoCapacity),
            "power_status",b.power==null?null:b.power.status,"power_net_in",b.sense(LAccess.powerNetIn),"power_net_out",b.sense(LAccess.powerNetOut),
            "power_links",links,"power_links_frame","absolute"));
        if(!b.block.privileged)out.put("config",config(b.config(),b.tileX(),b.tileY()));
        return out;
    }
    static Object config(Object value,int x,int y){
        if(value==null||value instanceof String||value instanceof Boolean)return value;
        if(value instanceof Float||value instanceof Double)return obj("type","float","value",value);
        if(value instanceof Number)return value;
        if(value instanceof MappableContent c)return obj("type",switch(c.getContentType()){case unit->"unit";case unitCommand->"command";case unitStance->"stance";default->c.getContentType().name();},"name",c.name);
        // An unlinked bridge reports its empty link (-1) as a point off the map.
        if(value instanceof Point2 p)return Vars.world.tile(x+p.x,y+p.y)==null?null:obj("type","point","frame","relative","x",p.x,"y",p.y);
        if(value instanceof byte[] bytes)return obj("type","bytes","base64",Base64.getEncoder().encodeToString(bytes));
        if(value instanceof int[] ints){ArrayList<Integer> out=new ArrayList<>(ints.length);for(int n:ints)out.add(n);return obj("type","ints","values",out);}
        if(value instanceof Point2[] points){ArrayList<Object> out=new ArrayList<>(points.length);for(Point2 p:points)out.add(obj("x",p.x+x,"y",p.y+y));return obj("type","points","frame","absolute","values",out);}
        throw new IllegalArgumentException("Unsupported native configuration "+value.getClass().getSimpleName());
    }
    Map<String,Object> block(Block b){
        ArrayList<Object> costs=new ArrayList<>(),configs=new ArrayList<>();for(ItemStack s:b.requirements)costs.add(obj("item",s.item.name,"amount",s.amount));for(Class<?> c:b.configurations.keys())configs.add(c.getSimpleName());
        return obj("name",b.name,"localized_name",b.localizedName,"description",CatalogText.description(b),"size",b.size,"footprint_offset",b.sizeOffset,"health",b.health,"category",b.category.name(),"cost",costs,"build_time",b.buildTime,"config_types",configs,"mechanics",mechanics(b));
    }
    Map<String,Object> unitType(UnitType u){
        ArrayList<Object> weapons=new ArrayList<>();
        for(var weapon:u.weapons) weapons.add(obj("name",weapon.name,"range_tiles",weapon.range()/8,"direct_damage",weapon.bullet.damage,"can_hit_air",weapon.bullet.collidesAir,"can_hit_ground",weapon.bullet.collidesGround,"player_controllable",weapon.controllable));
        ArrayList<String> commands=new ArrayList<>();
        for(var command:u.commands) commands.add(command.name);
        return obj("name",u.name,"description",CatalogText.description(u),"speed",u.speed,"health",u.health,"armor",u.armor,"flying",u.flying,"build_speed",u.buildSpeed,"build_range_tiles",u.buildRange/8,"mine_tier",u.mineTier,"mine_range_tiles",u.mineRange/8,"mine_floor",u.mineFloor,"mine_walls",u.mineWalls,"item_capacity",u.itemCapacity,"weapons",weapons,"commands",commands);
    }
    Object mechanics(Block b){
        LinkedHashMap<String,Object> result=obj("item_capacity",b.itemCapacity,"liquid_capacity",b.liquidCapacity);
        ArrayList<Object> consumes=new ArrayList<>();
        for(var c:b.consumers){Object detail=null;
            if(c instanceof mindustry.world.consumers.ConsumeItems ci){ArrayList<Object> is=new ArrayList<>();for(ItemStack st:ci.items)is.add(obj("item",st.item.name,"amount",st.amount));detail=obj("items",is);}
            else if(c instanceof mindustry.world.consumers.ConsumeItemFilter cf){ArrayList<Object> accepted=new ArrayList<>();for(Item i:Vars.content.items())if(cf.filter.get(i))accepted.add(obj("item",i.name,"efficiency_multiplier",cf.itemEfficiencyMultiplier(i)));detail=obj("accepted_items",accepted);}
            else if(c instanceof mindustry.world.consumers.ConsumeLiquid cl)detail=obj("liquid",cl.liquid.name,"per_second",cl.amount*60);
            else if(c instanceof mindustry.world.consumers.ConsumePower cp)detail=obj("power_per_second",cp.usage*60,"buffer_capacity",cp.capacity);
            consumes.add(obj("type",c.getClass().getSimpleName(),"optional",c.optional,"details",detail,"details_available",detail!=null));
        }
        result.put("consumes",consumes);
        if(b instanceof mindustry.world.blocks.power.PowerGenerator g)result.put("base_power_production_per_second",g.powerProduction*60);
        if(b instanceof mindustry.world.blocks.power.ConsumeGenerator g){result.put("item_duration_seconds",g.itemDuration/60);ArrayList<Object> durations=new ArrayList<>();for(var entry:g.itemDurationMultipliers)durations.add(obj("item",entry.key.name,"duration_multiplier",entry.value));result.put("item_duration_multipliers",durations);}
        if(b instanceof mindustry.world.blocks.production.GenericCrafter g){result.put("craft_seconds",g.craftTime/60);ArrayList<Object> outputs=new ArrayList<>();if(g.outputItems!=null)for(ItemStack st:g.outputItems)outputs.add(obj("item",st.item.name,"amount",st.amount));if(g.outputLiquids!=null)for(LiquidStack st:g.outputLiquids)outputs.add(obj("liquid",st.liquid.name,"per_second",st.amount*60));result.put("outputs",outputs);}
        if(b instanceof mindustry.world.blocks.production.Drill d){result.put("mine_tier",d.tier);result.put("drill_seconds",d.drillTime/60);}
        if(b instanceof mindustry.world.blocks.defense.turrets.BaseTurret t)result.put("range_tiles",t.range/8);
        if(b instanceof mindustry.world.blocks.defense.turrets.Turret t){result.put("target_air",t.targetAir);result.put("target_ground",t.targetGround);}
        if(b instanceof mindustry.world.blocks.defense.turrets.ItemTurret t){ArrayList<Object> ammo=new ArrayList<>();for(var e:t.ammoTypes)ammo.add(obj("item",e.key.name,"damage",e.value.damage,"ammo_multiplier",e.value.ammoMultiplier));result.put("ammo",ammo);}
        if(b instanceof mindustry.world.blocks.defense.turrets.LiquidTurret t){ArrayList<Object> ammo=new ArrayList<>();for(var e:t.ammoTypes)ammo.add(obj("liquid",e.key.name,"damage",e.value.damage));result.put("ammo",ammo);}
        if(b instanceof mindustry.world.blocks.units.UnitFactory f){ArrayList<Object> plans=new ArrayList<>();int index=0;for(var plan:f.plans){ArrayList<Object> costs=new ArrayList<>();for(ItemStack st:plan.requirements)costs.add(obj("item",st.item.name,"amount",st.amount));plans.add(obj("config_index",index++,"unit",plan.unit.name,"seconds",plan.time/60,"cost",costs));}result.put("unit_plans",plans);}
        return result;
    }
}
