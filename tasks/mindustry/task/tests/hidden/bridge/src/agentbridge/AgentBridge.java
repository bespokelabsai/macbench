// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.Core;
import arc.Events;
import arc.math.Mathf;
import arc.math.geom.Vec2;
import arc.struct.Seq;
import arc.util.Log;
import arc.util.Time;
import arc.util.serialization.JsonReader;
import arc.util.serialization.JsonValue;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import mindustry.Vars;
import mindustry.ai.WaveSpawner;
import mindustry.content.Blocks;
import mindustry.content.Planets;
import mindustry.core.GameState.State;
import mindustry.game.EventType.*;
import mindustry.game.Rules;
import mindustry.game.Team;
import mindustry.gen.*;
import mindustry.mod.Plugin;
import mindustry.net.Administration.Config;
import mindustry.net.NetConnection;
import mindustry.server.ServerControl;
import mindustry.type.Category;
import mindustry.type.Item;
import mindustry.type.ItemStack;
import mindustry.world.*;
import mindustry.world.meta.BuildVisibility;
import java.io.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public class AgentBridge extends Plugin {
    enum Phase {waiting,running,lost,frozen}
    final LinkedHashMap<String,Seat> seats=new LinkedHashMap<>();
    final Records records=new Records(this);
    final MatchLog log=new MatchLog(this);
    final Inputs inputs=new Inputs(this);
    final ScoreCounters score=new ScoreCounters(this);
    boolean configured;
    Phase phase=Phase.waiting;
    int worldIndex;
    WorldGen.Profile profile;
    HttpServer http;

    static class BotConnection extends NetConnection {
        BotConnection(Player p,String id){super("127.0.0.1");player=p;uuid="agent-bridge-"+id;usid=uuid;hasConnected=true;hasBegunConnecting=true;}
        @Override public void send(Object object,boolean reliable){}
        @Override public void close(){hasDisconnected=true;}
        @Override public boolean isConnected(){return !hasDisconnected;}
    }
    static class Seat {
        final String name;
        final int index;
        final Player player;
        Float moveX,moveY;
        float moveDx,moveDy;
        final Vec2 movement=new Vec2();
        Seat(String name,int index,Player p){this.name=name;this.index=index;player=p;}
        void release(){
            moveX=moveY=null;moveDx=moveDy=0;
            player.shooting=player.boosting=false;
            if(!player.dead()){player.unit().mineTile(null);player.unit().vel.setZero();}
        }
    }
    final class Spawner extends WaveSpawner {
        @Override public void spawnEnemies(){
            Vars.state.rules.spawns=ProgressiveWaves.groups(Vars.state.wave,getSpawns(),profile.firstBudget(),profile.growth());
            super.spawnEnemies();
        }
    }
    @Override public void init(){
        // Runs before ServerLoadEvent, so the command thread that reads stdin never starts.
        ServerControl.instance.serverInput=null;
        ServerControl.instance.gameOverListener=event->lost();
        try{
            http=HttpServer.create(new InetSocketAddress("127.0.0.1",8091),32);
            http.createContext("/",this::request);
            http.setExecutor(Executors.newFixedThreadPool(8));http.start();
        }catch(Exception e){throw new RuntimeException(e);}
        Events.on(ServerLoadEvent.class,event->{Config.autoPause.set(false);Config.autosave.set(false);Config.antiSpam.set(false);});
        log.listen();
        score.install();
        Vars.spawner=new Spawner();
        Events.run(Trigger.beforeGameUpdate,this::controls);
        Events.run(Trigger.beforeGameUpdate,score::poll);
        Events.run(Trigger.afterGameUpdate,log::pollCore);
        Log.info("Agent bridge for Mindustry @ listening on loopback :8091",mindustry.core.Version.buildString());
    }
    void controls(){
        if(phase!=Phase.running)return;
        for(Seat s:seats.values()){
            Player p=s.player;
            if(p.dead()){s.release();continue;}
            Unit u=p.unit();
            if(s.moveX!=null){
                Vec2 delta=s.movement.set(s.moveX-u.x,s.moveY-u.y);
                if(delta.len()<2f){s.moveX=s.moveY=null;u.vel.setZero();}
                else u.movePref(delta.limit(Math.min(u.speed(),delta.len()/Math.max(Time.delta,0.01f))));
            }else{
                u.movePref(s.movement.set(s.moveDx,s.moveDy).scl(u.speed()));
            }

            u.move(u.vel.x*Time.delta,u.vel.y*Time.delta);

            boolean boosted=u instanceof Mechc&&u.isFlying();
            boolean aimCursor=u.type.omniMovement&&p.shooting&&u.type.hasWeapons()&&u.type.faceTarget&&!boosted;
            u.lookAt(aimCursor?u.angleTo(p.mouseX,p.mouseY):u.prefRotation());
            u.snapInterpolation();
            u.aim(p.mouseX,p.mouseY,true);u.controlWeapons(true,p.shooting&&!boosted);
        }
    }
    void request(HttpExchange ex) throws IOException {
        LinkedHashMap<String,Object> response;int status=200;
        Object[] seatStatus={null};
        String path=ex.getRequestURI().getPath();
        try{
            require(ex.getRequestMethod().equals("POST"),"POST required");
            byte[] body=ex.getRequestBody().readAllBytes();
            require(body.length>0,"JSON body required");
            JsonValue data;
            try{data=new JsonReader().parse(new String(body,StandardCharsets.UTF_8));}
            catch(RuntimeException error){throw new IllegalArgumentException("Malformed JSON body",error);}
            CompletableFuture<Object> task=new CompletableFuture<>();
            Core.app.post(()->{
                Object result=null;Throwable error=null;
                try{result=route(path,data);}catch(Throwable e){error=e;}

                try{Seat seat=statusSeat(path,data);if(seat!=null)seatStatus[0]=log.status(seat);}catch(Throwable e){if(error==null)error=e;}
                if(error!=null)task.completeExceptionally(error);else task.complete(result);
            });
            response=obj("ok",true,"result",task.get(25,TimeUnit.SECONDS));
        }catch(Exception e){
            Throwable cause=e instanceof ExecutionException?e.getCause():e;
            if(cause instanceof IllegalArgumentException||cause instanceof Refused)
                response=obj("ok",false,"error",cause.getMessage(),"invalid",cause instanceof IllegalArgumentException);
            else{status=500;Log.err(cause);response=obj("ok",false,"error",cause.getClass().getSimpleName()+": "+cause.getMessage());}
        }
        if(seatStatus[0]!=null)response.put("status",seatStatus[0]);
        byte[] output=json(response).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type","application/json");
        ex.sendResponseHeaders(status,output.length);ex.getResponseBody().write(output);ex.close();
        if(path.equals("/stop")&&status==200&&Boolean.TRUE.equals(response.get("ok")))System.exit(0);
    }
    Object route(String path,JsonValue a){
        require(a!=null&&a.isObject(),"Request must be an object");
        return switch(path){
            case "/health"->{Records.only(a);yield obj("ready",true,"configured",configured,"version",mindustry.core.Version.buildString());}
            case "/setup"->setup(a);
            case "/seat"->{
                Records.only(a,"seat");Records.field(a,"seat","string",true);
                require(configured,"World is not set up");
                String name=a.getString("seat");boolean added=!seats.containsKey(name);
                if(added)addSeat(name);
                yield obj("seat",name,"added",added);
            }
            case "/start"->{
                Records.only(a);require(configured,"World is not set up");
                if(phase==Phase.waiting){phase=Phase.running;Vars.state.set(State.playing);}
                yield obj("phase",phase.name());
            }
            case "/score"->{Records.only(a);yield scoreRow();}
            case "/freeze"->{
                Records.only(a);require(configured,"World is not set up");
                if(phase!=Phase.lost)phase=Phase.frozen;
                Vars.state.set(State.paused);
                yield obj("phase",phase.name());
            }
            case "/stop"->{Records.only(a);yield obj("stopped",true);}
            case "/status"->{Records.only(a,"seat");yield log.status(seat(a));}
            case "/query"->records.query(seat(a),a);
            case "/events"->log.events(seat(a),a);
            case "/act"->inputs.act(seat(a),a);
            default->throw new IllegalArgumentException("Unknown endpoint "+path);
        };
    }
    Seat statusSeat(String path,JsonValue a){
        if(!configured||path.equals("/status")||a==null||!a.isObject()||!a.has("seat")||!a.get("seat").isString())return null;
        return seats.get(a.getString("seat"));
    }
    Seat seat(JsonValue a){
        Records.field(a,"seat","string",true);
        require(configured,"World is not set up");
        Seat seat=seats.get(a.getString("seat"));require(seat!=null,"Unknown seat "+a.getString("seat"));return seat;
    }
    Object setup(JsonValue a){
        Records.only(a,"world_index","seed","seats","profile");
        require(!configured,"World is already set up");
        Records.field(a,"world_index","int",true);require(a.has("seed")&&a.get("seed").isNumber(),"seed must be a number");
        JsonValue names=a.get("seats");require(names!=null&&names.isArray(),"seats must be an array");
        LinkedHashSet<String> requested=new LinkedHashSet<>();
        for(JsonValue name:names){require(name.isString()&&!name.asString().isEmpty(),"Seat names must be nonempty strings");require(requested.add(name.asString()),"Duplicate seat");}
        profile=WorldGen.Profile.parse(a.get("profile"));
        worldIndex=a.getInt("world_index");
        long seed=a.getLong("seed");Mathf.rand.setSeed(seed);
        WorldGen.Layout layout=WorldGen.generate(seed,profile);
        load(layout);
        for(String name:requested)addSeat(name);
        Vars.state.set(State.paused);configured=true;
        return obj("phase",phase.name(),"map_hash",layout.hash());
    }
    Rules rules(){
        Rules r=new Rules();
        Planets.serpulo.applyRules(r,true);
        r.waves=true;r.waveTimer=true;r.waitEnemies=false;r.waveSending=false;
        r.winWave=0;r.attackMode=false;r.pvp=false;r.canGameOver=true;r.coreIncinerates=true;
        r.waveSpacing=profile.waveSpacingTicks();r.initialWaveSpacing=profile.firstWaveTicks();
        r.weather.clear();r.spawns.clear();
        r.loadout=new Seq<>();
        for(var entry:profile.loadout().entrySet()){
            Item item=Vars.content.item(entry.getKey());require(item!=null,"profile.loadout: unknown item "+entry.getKey());
            r.loadout.add(new ItemStack(item,entry.getValue()));
        }
        for(Block b:Vars.content.blocks())if(b.category==Category.logic||b.privileged||b.buildVisibility==BuildVisibility.sandboxOnly)r.bannedBlocks.add(b);
        return r;
    }
    void load(WorldGen.Layout l){
        Rules r=rules();
        Vars.logic.reset();
        Vars.state.rules=r;
        Vars.world.loadGenerator(l.size(),l.size(),tiles->{
            for(int y=0;y<l.size();y++)for(int x=0;x<l.size();x++){
                int t=l.at(x,y);
                tiles.set(x,y,new Tile(x,y,Vars.content.block(l.floor()[t]),Vars.content.block(l.overlay()[t]),Vars.content.block(l.wall()[t])));
            }
            tiles.getn(l.coreX(),l.coreY()).setBlock(Blocks.coreShard,r.defaultTeam);
        });
        Vars.state.rules=r;
        Events.fire(new RulesLoadEvent(r));Vars.logic.play();
    }
    void addSeat(String name){
        Player p=Player.create();p.name=name;p.admin=false;p.con=new BotConnection(p,name);
        p.team(Vars.state.rules.defaultTeam);p.color.set(p.team().color);p.add();p.checkSpawn();
        seats.put(name,new Seat(name,seats.size()+1,p));log.addSeat(name);
    }
    void lost(){
        if(phase==Phase.lost)return;
        phase=Phase.lost;Vars.state.set(State.paused);ServerControl.instance.cancelPlayTask();
        log.coreLost();
    }
    LinkedHashMap<String,Object> scoreRow(){
        Team team=Vars.state.rules.waveTeam;
        return obj("phase",phase.name(),"tick",configured?Vars.state.updateId:0,"sim_ticks",configured?Vars.state.tick:0,
            "heap_mb",(Runtime.getRuntime().totalMemory()-Runtime.getRuntime().freeMemory())/1048576,"wave",configured?Vars.state.wave:0,
            "core_alive",configured&&Vars.state.rules.defaultTeam.core()!=null,
            "enemies_alive",configured?Groups.unit.count(u->u.team==team):0,
            "counters",obj("source",new TreeMap<>(score.source),"core_in",new TreeMap<>(score.coreIn),"core_in_tainted",new TreeMap<>(score.coreInTainted)));
    }
    /** Sight centre in tiles: the seat's unit, or its team's core while it has no unit; null when it has neither. */
    Vec2 center(Seat s){
        Player p=s.player;
        if(!p.dead())return new Vec2(p.unit().x/8,p.unit().y/8);
        Building core=p.team().core();
        return core==null?null:new Vec2(core.x/8,core.y/8);
    }
    boolean sees(Seat s,float x,float y){
        Vec2 c=center(s);
        return c!=null&&c.dst2(x,y)<=profile.sightRadius()*profile.sightRadius();
    }

    static final class Refused extends RuntimeException{Refused(String message){super(message);}}
    static void require(boolean test,String error){if(!test)throw new IllegalArgumentException(error);}

    static String list(Collection<?> values){StringJoiner out=new StringJoiner(", ");for(Object value:values)out.add(String.valueOf(value));return out.toString();}
    static LinkedHashMap<String,Object> obj(Object... pairs){LinkedHashMap<String,Object> m=new LinkedHashMap<>();for(int i=0;i<pairs.length;i+=2)m.put((String)pairs[i],pairs[i+1]);return m;}
    static String json(Object v){if(v==null)return "null";if(v instanceof Number n)return Double.isFinite(n.doubleValue())?n.toString():"null";if(v instanceof Boolean)return v.toString();if(v instanceof Map<?,?> m){ArrayList<String> p=new ArrayList<>();for(var e:m.entrySet())p.add(json(e.getKey().toString())+":"+json(e.getValue()));return "{"+String.join(",",p)+"}";}if(v instanceof Iterable<?> a){ArrayList<String> p=new ArrayList<>();for(Object x:a)p.add(json(x));return "["+String.join(",",p)+"]";}String s=v.toString();StringBuilder b=new StringBuilder("\"");for(char c:s.toCharArray()){switch(c){case '"'->b.append("\\\"");case '\\'->b.append("\\\\");case '\n'->b.append("\\n");case '\r'->b.append("\\r");case '\t'->b.append("\\t");default->{if(c<32)b.append(String.format("\\u%04x",(int)c));else b.append(c);}}}return b.append('"').toString();}
}
