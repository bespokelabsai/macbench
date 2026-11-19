// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.Events;
import arc.util.serialization.JsonValue;
import mindustry.Vars;
import mindustry.game.EventType.*;
import mindustry.game.Team;
import mindustry.gen.*;
import java.util.*;
import java.util.function.Predicate;
import static agentbridge.AgentBridge.*;
import static agentbridge.Records.*;

/**
 * Per-seat event logs. An entry is added to a seat's log only if the seat may see it at the moment it is logged, so filters and
 * paging run over visible rows alone. Own-team building events and game-wide facts (wave number, core health) reach every seat;
 * entries that name a unit reach the seats that see that unit; act entries reach the acting seat only.
 */
final class MatchLog {
    final AgentBridge bridge;
    final LinkedHashMap<String,View> views=new LinkedHashMap<>();
    // A seat's log is bounded, so its memory and the cost of reading it do not grow with the length of the run.
    static final int MAX_ENTRIES=4000,TRIM=1000,MAX_CURSORS=32,MAX_ARGS_CHARS=2048;
    float lastCoreHealth=-1;
    long lastCoreEvent;
    static final class Cursor {
        final List<Records.Predicate> filter;
        final Object selection;
        int position;
        Cursor(List<Records.Predicate> filter,Object selection){this.filter=filter;this.selection=selection;}
    }
    static final class View {
        final ArrayList<Map<String,Object>> entries=new ArrayList<>();
        int dropped;   // entries before the first one kept; seq stays absolute
        final Map<String,Cursor> cursors=new HashMap<>();
    }
    MatchLog(AgentBridge bridge){this.bridge=bridge;}

    void addSeat(String seat){views.put(seat,new View());}
    int head(String seat){View v=views.get(seat);return v.dropped+v.entries.size();}

    Map<String,Object> status(AgentBridge.Seat seat){
        Player p=seat.player;Team team=p.team();
        Unit u=p.dead()?null:p.unit();Building core=team.core();
        var center=bridge.center(seat);
        return obj("phase",bridge.phase.name(),"tick",Vars.state.updateId,"world_index",bridge.worldIndex,"team",team.name,
            "map",obj("width",Vars.world.width(),"height",Vars.world.height()),
            "sight_radius_tiles",bridge.profile.sightRadius(),
            "center",center==null?null:obj("x",center.x,"y",center.y),
            "you",u==null?null:obj("unit_id",u.id,"type",u.type.name,"health",u.health,"x",u.x/8,"y",u.y/8,"stack",stack(u),
                "mining",u.mineTile==null?null:obj("x",(int)u.mineTile.x,"y",(int)u.mineTile.y)),
            "core",core==null?null:obj("id",core.id,"x",core.tileX(),"y",core.tileY(),"health",core.health,"max_health",core.maxHealth,"items",items(core)),
            "wave",obj("number",Vars.state.wave,"next_in_seconds",Vars.state.wavetime/60));
    }
    void append(String kind,Map<String,Object> fields,Predicate<AgentBridge.Seat> audience){
        if(!bridge.configured)return;
        LinkedHashMap<String,Object> data=obj("tick",Vars.state.updateId,"kind",kind);data.putAll(fields);
        for(var seat:bridge.seats.values())if(audience.test(seat)){
            View view=views.get(seat.name);view.entries.add(data);
            if(view.entries.size()>MAX_ENTRIES+TRIM){view.entries.subList(0,TRIM).clear();view.dropped+=TRIM;}
        }
    }
    boolean own(Team team){return team==Vars.state.rules.defaultTeam;}
    /** The building's team sees it anywhere; other seats only inside their sight. */
    void building(String kind,Map<String,Object> fields,Team team,Building b,mindustry.world.Tile tile){
        float x=b!=null?b.x/8:tile.x,y=b!=null?b.y/8:tile.y;
        append(kind,fields,seat->own(team)||bridge.sees(seat,x,y));
    }
    void unit(String kind,Map<String,Object> fields,Unit u){
        if(u==null)return;
        float x=u.x/8,y=u.y/8;
        append(kind,fields,seat->bridge.sees(seat,x,y));
    }
    void listen(){
        Events.on(BlockBuildBeginEvent.class,e->building("block_build_begin",obj("tile",ref(e.tile),"team",e.team.name,"breaking",e.breaking),e.team,e.tile.build,e.tile));
        Events.on(BlockBuildEndEvent.class,e->building("block_build_end",obj("tile",ref(e.tile),"team",e.team.name,"breaking",e.breaking),e.team,e.tile.build,e.tile));
        Events.on(BlockDestroyEvent.class,e->building("block_destroy",obj("tile",ref(e.tile),"team",e.tile.team().name),e.tile.team(),e.tile.build,e.tile));
        Events.on(ConfigEvent.class,e->{if(e.tile!=null)building("config",obj("building",ref(e.tile),"value",config(e.tile.config(),e.tile.tileX(),e.tile.tileY())),e.tile.team,e.tile,e.tile.tile);});
        Events.on(BuildTeamChangeEvent.class,e->building("building_team_change",obj("previous",e.previous.name,"building",ref(e.build)),e.build.team,e.build,e.build.tile));
        Events.on(UnitCreateEvent.class,e->unit("unit_create",obj("unit",ref(e.unit)),e.unit));
        Events.on(UnitDestroyEvent.class,e->{
            for(var seat:bridge.seats.values())if(seat.player.unit()==e.unit)seat.release();
            unit("unit_destroy",obj("unit",ref(e.unit)),e.unit);
        });
        Events.on(UnitDrownEvent.class,e->unit("unit_drown",obj("unit",ref(e.unit)),e.unit));
        Events.on(UnitControlEvent.class,e->unit("unit_control",obj("player",e.player.name,"unit",ref(e.unit)),e.unit));
        Events.on(UnitChangeEvent.class,e->unit("unit_change",obj("player",e.player.name,"unit",ref(e.unit)),e.unit));
        Events.on(CoreChangeEvent.class,e->{if(own(e.core.team))append("core_change",obj("core",ref(e.core)),seat->true);});
        Events.on(WaveEvent.class,e->append("wave",obj("wave",Vars.state.wave),seat->true));
        Events.on(PickupEvent.class,e->{if(e.carrier!=null)unit("pickup",obj("carrier",ref(e.carrier)),e.carrier);});
        Events.on(PayloadDropEvent.class,e->{if(e.carrier!=null)unit("payload_drop",obj("carrier",ref(e.carrier)),e.carrier);});
    }
    void coreLost(){append("core_lost",obj(),seat->true);}
    /** Core damage has no usable native event on a headless server, so health is sampled at most once a second. */
    void pollCore(){
        Building core=Vars.state.rules.defaultTeam.core();
        if(core==null||bridge.phase!=AgentBridge.Phase.running)return;
        if(lastCoreHealth>=0&&core.health<lastCoreHealth&&Vars.state.updateId-lastCoreEvent>=60){
            lastCoreEvent=Vars.state.updateId;
            append("core_damage",obj("health",core.health,"max_health",core.maxHealth),seat->true);
            lastCoreHealth=core.health;
        }else if(core.health>lastCoreHealth)lastCoreHealth=core.health;
    }
    Map<String,Object> row(Map<String,Object> entry,int seq){LinkedHashMap<String,Object> result=new LinkedHashMap<>(entry);result.put("seq",seq);return result;}
    Object events(AgentBridge.Seat seat,JsonValue a){
        only(a,"seat","cursor","after","latest","filter","fields","limit");
        require((a.has("cursor")?1:0)+(a.has("after")?1:0)+(a.has("latest")?1:0)==1,"Exactly one of cursor, after, latest is required");
        List<String> fields=fields(a.get("fields"));
        View view=views.get(seat.name);
        if(a.has("latest")){
            require(!a.has("limit"),"latest excludes limit");
            require(wholeNumber(a.get("latest"))&&a.getDouble("latest")>0,"latest must be a positive integer");
            double count=a.getDouble("latest");List<Records.Predicate> filter=filter(a.get("filter"),"filter");
            ArrayList<Object> results=new ArrayList<>();int scanned=0;
            for(int i=view.entries.size()-1;i>=0&&results.size()<count;i--){
                Map<String,Object> row=row(view.entries.get(i),view.dropped+i+1);scanned++;
                if(matches(row,filter))results.add(project(row,fields));
            }
            Collections.reverse(results);return obj("events",results,"scanned",scanned);
        }
        double limit=limit(a),after;String name=null;Cursor cursor=null;List<Records.Predicate> filter;
        if(a.has("cursor")){
            field(a,"cursor","string",true);name=a.getString("cursor");
            require(!name.isEmpty(),"cursor must be a nonempty string");
            cursor=view.cursors.get(name);
            if(cursor==null){
                if(view.cursors.size()>=MAX_CURSORS)throw new Refused("At most "+MAX_CURSORS+" named readers per player");
                filter=filter(a.get("filter"),"filter");
                cursor=new Cursor(filter,a.has("filter")?plain(a.get("filter")):List.of());
            }else if(a.has("filter")){
                filter(a.get("filter"),"filter");
                if(!equal(cursor.selection,plain(a.get("filter"))))throw new Refused("The reader \""+name+"\" already has another filter; use a different cursor name");
            }
            filter=cursor.filter;after=cursor.position;
        }else{
            require(wholeNumber(a.get("after"))&&a.getDouble("after")>=0,"after must be a nonnegative integer");
            after=a.getDouble("after");filter=filter(a.get("filter"),"filter");
        }
        ArrayList<Object> results=new ArrayList<>();Object next=cursor==null?plain(a.get("after")):cursor.position;boolean more=false;
        int scanned=0;
        for(int i=(int)Math.max(0,Math.min(after,view.dropped+view.entries.size())-view.dropped);i<view.entries.size();i++){
            Map<String,Object> row=row(view.entries.get(i),view.dropped+i+1);scanned++;
            if(matches(row,filter)){
                if(results.size()==limit){more=true;break;}
                results.add(project(row,fields));
            }next=view.dropped+i+1;
        }
        if(cursor==null)return obj("events",results,"next",next,"more",more,"scanned",scanned);
        cursor.position=((Number)next).intValue();view.cursors.put(name,cursor);
        return obj("cursor",name,"events",results,"more",more,"scanned",scanned);
    }
    void action(AgentBridge.Seat seat,String op,JsonValue args,Object effect){
        Object kept=plain(args);
        if(AgentBridge.json(kept).length()>MAX_ARGS_CHARS)kept=obj("truncated",true);
        append("act",obj("seat",seat.name,"op",op,"args",kept,"effect",effect),s->s==seat);
    }
}
