// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.Events;
import arc.struct.Seq;
import mindustry.Vars;
import mindustry.content.Planets;
import mindustry.game.EventType.*;
import mindustry.gen.Building;
import mindustry.type.Item;
import mindustry.world.Block;
import mindustry.world.Edges;
import mindustry.world.Tile;
import mindustry.world.blocks.ConstructBlock;
import mindustry.world.blocks.distribution.*;
import mindustry.world.blocks.production.*;
import mindustry.world.blocks.storage.*;
import mindustry.world.blocks.units.*;
import mindustry.world.meta.BlockGroup;
import java.util.*;

/**
 * source[item] counts Building.offload on item-producing blocks; core_in[item] counts CoreBuild.handleItem from a building.
 * The build types are subclassed because the engine has no event for either. Checked against Mindustry v160.7:
 *
 * Building.offload has exactly these callers: Drill, BurstDrill, GenericCrafter (AttributeCrafter, HeatCrafter), Separator
 * and WallCrafter. Each call is one item made by a working drill or a finished recipe.
 *
 * These move items but never reach offload, so they leave source alone:
 *  - hand and unit mining (MinerComp -> Call.transferItemTo -> handleStack, or addItem on the unit);
 *  - deposits and withdrawals (transferInventory -> handleStack, takeItems -> removeStack);
 *  - construction refunds (ConstructBuild -> core.items.add, also no handleItem);
 *  - unloaders, conveyors, bridges, routers, mass drivers (dump or handleItem only);
 *  - BeamDrill and PayloadDeconstructor (items.add; Erekir only, and not placeable on Serpulo).
 *
 * These reach CoreBuild.handleItem without any production; the first is tainted (below), and the grade's min() is a second bound:
 *  - an unloader on the core or a linked container feeding back into the core;
 *  - ItemSource (sandbox, refused at plan_add).
 * A linked container forwards handleItem to the core, so it counts once.
 *
 * core_in leaves out what is tainted, so min() cannot pair real output with returning stock. A building is tainted when core stock can
 * reach it through transport (see recompute). Taint lasts for the life of the building object. It passes to the building that replaces it
 * in place only when ConstructBlock.constructFinish hands that building the old items through overwrote(): Conveyor and StackConveyor take a
 * conveyor's items, an unlinked StorageBlock takes every old building's items, and no other Serpulo transport block overrides overwrote.
 * The marker lives as long as the construct site; a site destroyed, deconstructed or replaced drops it. Deconstructing drops the contents,
 * so a building rebuilt from nothing is clean. Tainted arrivals are counted in core_in_tainted for diagnostics only.
 *
 * Every native way stock can leave a core or enter transport, checked in v160.7:
 *  - Unloader on a core or linked container: an injector, seeded in recompute.
 *  - Unloader on a consumer can pull its inputs, whatever fed them (Unloader.updateTile takes any item the building holds, and only
 *    conveyors, routers, junctions, sorters, gates and bridges are unloadable=false): an injector if it can take an input. One set to an
 *    output item of the building is clean, since consumers accept only inputs. An unset one can take inputs, so it is an injector.
 *  - deposit: AllowedDeposit in Inputs refuses all transport and storage. Consumers do not dump inputs (GenericCrafter.dumpOutputs,
 *    Separator.canDump), and the rest have no item output.
 *  - Unit AI (MinerAI, PrebuildAI) transfers only into cores. CargoAI and payload loaders/unloaders, deconstructors are Erekir and not placeable. LExecutor is banned with logic blocks.
 *  - Mass drivers and bridges: followed through their link while it is valid (ItemBridge.linkValid, MassDriverBuild.linkValid); a stale
 *    link to a destroyed block is not followed. They take and dump items only as the engine allows (see receives and dumps).
 *    Unloaders pull from any unloadable building (mass drivers, plastanium conveyors, containers), whatever its links. Payload pickup
 *    keeps the building object, so a carried container stays tainted; linked containers cannot be picked up (StorageBuild.canPickup).
 *  - Core-linked storage has the core's own inventory; items arriving there are arrivals at the core.
 *
 * Crafter inputs may come from any stock (hand-mined, starting items, refunds). The output is still a real recipe and counts
 * as source: crafting consumes the input, and no Serpulo recipe returns its own input, so stock can not be cycled.
 */
final class ScoreCounters {
    final AgentBridge bridge;
    final TreeMap<String,Long> source=new TreeMap<>(),coreIn=new TreeMap<>(),coreInTainted=new TreeMap<>();
    /** Tainted buildings, each with the items that can be core stock on it (bit item.id). Any arrival from one is tainted; the items
     *  only decide where a sorter or an unloader passes core stock on. */
    final Map<Building,Long> tainted=new IdentityHashMap<>();
    /** Junctions and sorters route by the direction items enter (Junction.updateTile, Sorter.getTileTarget), so they are tainted per lane:
     *  lanes[d] holds the items that can be core stock entering heading d. A clean stream crossing a tainted one stays clean. */
    final Map<Building,long[]> lanes=new IdentityHashMap<>();
    /** Construct sites, by tile, whose old buildings may hand core stock to the finished building. */
    final Map<Integer,ConstructBlock.ConstructBuild> inheriting=new HashMap<>();
    boolean dirty=true;
    ScoreCounters(AgentBridge bridge){this.bridge=bridge;}

    void countSource(Building b,Item item){
        if(bridge.phase==AgentBridge.Phase.running&&b.team==Vars.state.rules.defaultTeam)source.merge(item.name,1L,Long::sum);
    }
    void countCoreIn(Building core,Building from,Item item){
        if(bridge.phase==AgentBridge.Phase.running&&from!=null&&core.team==Vars.state.rules.defaultTeam)(tainted(from,core)?coreInTainted:coreIn).merge(item.name,1L,Long::sum);
    }

    /** Whether what from hands to the core may be core stock. A linked container passes its source on to the core. */
    boolean tainted(Building from,Building core){
        if(tainted.containsKey(from))return true;
        long[] m=lanes.get(from);
        if(m!=null)for(int lane=0;lane<4;lane++)for(int d=0;d<4;d++)
            if(toward(from,lane,m[lane],d)!=0&&(from.nearby(d)==core||from.nearby(d) instanceof StorageBlock.StorageBuild s&&s.linkedCore==core))return true;
        return false;
    }
    static long bit(Item item){return 1L<<item.id;}
    /** Items of m that enter junction or sorter b heading lane and leave it heading d. A sorter sends an item forward when
     *  ((item==sortItem)!=invert)==enabled and to a side otherwise; a junction sends everything straight on. */
    static long toward(Building b,int lane,long m,int d){
        if(!(b instanceof Sorter.SorterBuild s))return d==lane?m:0;
        long forward=s.sortItem==null?0:bit(s.sortItem);
        if(((Sorter)b.block).invert==s.enabled)forward=~forward;
        return d==lane?m&forward:(d+lane)%2==1?m&~forward:0;
    }

    void poll(){if(dirty||Vars.state.updateId%30==0)recompute();}
    /** Items that can be core stock on a building, or on one lane of a junction or sorter when lane is not -1. */
    record Step(Building b,int lane,long items){}
    /** Walks downstream from every injector and every building or lane already tainted, stopping at consumers and cores. Over-approximates. */
    void recompute(){
        dirty=false;
        inheriting.values().removeIf(site->site.tile.build!=site);
        ArrayDeque<Step> queue=new ArrayDeque<>();
        tainted.forEach((b,m)->queue.add(new Step(b,-1,m)));
        lanes.forEach((b,m)->{for(int d=0;d<4;d++)if(m[d]!=0)queue.add(new Step(b,d,m[d]));});
        for(Building b:Vars.state.rules.defaultTeam.data().buildings)if(b instanceof Unloader.UnloaderBuild u&&injected(u)!=0)queue.add(new Step(u,-1,injected(u)));
        Map<Building,long[]> seen=new IdentityHashMap<>();
        while(!queue.isEmpty()){
            Step s=queue.poll();
            Building b=s.b();
            long[] done=seen.computeIfAbsent(b,k->new long[5]);
            int slot=s.lane()==-1?4:s.lane();
            long m=s.items()&~done[slot];
            if(m==0)continue;
            done[slot]|=m;
            if(s.lane()!=-1)lanes.computeIfAbsent(b,k->new long[4])[s.lane()]|=m;else tainted.merge(b,m,(x,y)->x|y);
            if(b.tile==null||b.tile.build!=b)continue;
            if(s.lane()!=-1){for(int d=0;d<4;d++)push(queue,b.nearby(d),b,toward(b,s.lane(),m,d));continue;}
            if(b instanceof Conveyor.ConveyorBuild c){push(queue,c.next,c,m);continue;}
            // A plastanium conveyor hands its stack only to the next segment (StackConveyor.updateTile), and only the end segment dumps.
            boolean stack=b instanceof StackConveyor.StackConveyorBuild c&&!(c.state==2&&((StackConveyor)b.block).outputRouter);
            if(stack&&b.front() instanceof StackConveyor.StackConveyorBuild next)push(queue,next,b,m);
            for(Building next:b.proximity){
                // Unloaders pull from any unloadable building (Unloader.updateTile). Native unloaders never load storage, and storage outputs only through unloaders.
                if(next instanceof Unloader.UnloaderBuild?b.canUnload():!stack&&!linkedOnly(b)&&dumps(b,next)&&!(b instanceof StorageBlock.StorageBuild)&&!(b instanceof Unloader.UnloaderBuild&&next instanceof StorageBlock.StorageBuild))
                    push(queue,next,b,m);
            }
            push(queue,b instanceof ItemBridge.ItemBridgeBuild bridge&&linkedOnly(b)?Vars.world.build(bridge.link):b instanceof MassDriver.MassDriverBuild driver?target(driver):null,b,m);
        }
    }
    /** Queues what `from` can hand items to. A junction or sorter is entered on the lane the engine picks: from.relativeTo(it)
     *  (Junction.handleItem, Sorter.getTileTarget). An unloader set to an item takes only that item. */
    static void push(ArrayDeque<Step> queue,Building next,Building from,long items){
        if(items==0||next==null||!transport(next)||!receives(next,from))return;
        if(next instanceof Unloader.UnloaderBuild u&&u.sortItem!=null)items&=bit(u.sortItem);
        boolean laned=next instanceof Junction.JunctionBuild||next instanceof Sorter.SorterBuild;
        int lane=laned?from.relativeTo(next.tile):-1;
        if(lane!=-1||!laned)queue.add(new Step(next,lane,items));
    }
    /** A bridge with a valid link only sends over it (ItemBridge.updateTile). A mass driver with a valid link dumps only while idle or
     *  accepting (MassDriver.updateTile), which needs another driver shooting at it, so it counts as linked only when none links to it. */
    static boolean linkedOnly(Building b){
        if(b instanceof ItemBridge.ItemBridgeBuild bridge)return ((ItemBridge)b.block).linkValid(b.tile,Vars.world.tile(bridge.link));
        if(!(b instanceof MassDriver.MassDriverBuild driver)||!driver.waitingShooters.isEmpty())return false;
        if(target(driver)==null)return false;
        for(Building s:b.team.data().buildings)if(s instanceof MassDriver.MassDriverBuild shooter&&shooter.link==b.pos())return false;
        return true;
    }
    /** The driver a mass driver shoots at, or null when its link is not valid (MassDriverBuild.linkValid). */
    static MassDriver.MassDriverBuild target(MassDriver.MassDriverBuild driver){
        return driver.link!=-1&&Vars.world.build(driver.link) instanceof MassDriver.MassDriverBuild other&&other.block==driver.block&&other.team==driver.team&&driver.within(other,((MassDriver)driver.block).range)?other:null;
    }
    /** A bridge without a valid link dumps to every side except those its incoming bridges sit on (ItemBridgeBuild.checkDump). A side
     *  counts as incoming only while the engine lists that bridge and it is still linked here (checkIncoming), so a stale entry does not hide a side. */
    static boolean dumps(Building b,Building to){
        if(!(b instanceof ItemBridge.ItemBridgeBuild bridge))return true;
        Tile edge=Edges.getFacingEdge(to.tile,b.tile);
        int side=b.relativeTo(edge.x,edge.y);
        for(int i=0;i<bridge.incoming.size;i++){
            Tile in=Vars.world.tile(bridge.incoming.items[i]);
            if(in!=null&&b.relativeTo(in.x,in.y)==side&&in.build instanceof ItemBridge.ItemBridgeBuild src&&src.link==b.pos()&&((ItemBridge)b.block).linkValid(b.tile,in,false))return false;
        }
        return true;
    }
    /** Items an unloader can take out of core stock or out of a consumer's inputs. Any unloader on a core or linked container counts. */
    static long injected(Unloader.UnloaderBuild u){
        long m=0;
        for(Building other:u.proximity)m|=other instanceof CoreBlock.CoreBuild||other instanceof StorageBlock.StorageBuild s&&s.linkedCore!=null?-1L:inputs(other);
        return u.sortItem==null?m:m&bit(u.sortItem);
    }
    static long inputs(Building other){
        if(other.items==null||!other.block.unloadable||transport(other))return 0;
        long m=0;
        // UnitFactory consumes through ConsumeItemDynamic, which leaves consumesItem false; it holds only items of its plans (acceptItem).
        for(Item item:Vars.content.items())if(other.block instanceof UnitFactory factory?factory.capacities[item.id]>0:other.block.consumesItem(item))m|=bit(item);
        return m;
    }
    static boolean transport(Building b){
        if(b instanceof CoreBlock.CoreBuild||b instanceof StorageBlock.StorageBuild s&&s.linkedCore!=null)return false;
        return b.block.group==BlockGroup.transportation||b instanceof Unloader.UnloaderBuild||b instanceof MassDriver.MassDriverBuild||b instanceof StorageBlock.StorageBuild;
    }
    /** A conveyor pointing into a building cannot take items from it. A plastanium conveyor takes items only as a loading end and not from
     *  its front (StackConveyor.acceptItem), or as a whole stack from the segment behind it. A bridge takes items from a bridge validly linked
     *  to it, and otherwise only while its own link is valid and not from the side facing that link (ItemBridgeBuild.checkAccept). A mass driver
     *  takes shots from a driver validly linked to it, and other items only while its own link is valid (MassDriverBuild.acceptItem). */
    static boolean receives(Building next,Building from){
        if(next instanceof ItemBridge.ItemBridgeBuild bridge){
            ItemBridge block=(ItemBridge)next.block;
            if(from instanceof ItemBridge.ItemBridgeBuild f&&f.link==next.pos()&&block.linkValid(from.tile,next.tile))return true;
            Tile link=Vars.world.tile(bridge.link);
            if(!block.linkValid(next.tile,link))return false;
            Tile facing=Edges.getFacingEdge(from,next);
            return next.relativeTo(link)!=(facing==null?-1:next.relativeTo(facing));
        }
        if(next instanceof MassDriver.MassDriverBuild driver)return from instanceof MassDriver.MassDriverBuild f&&target(f)==driver||target(driver)!=null;
        if(next instanceof StackConveyor.StackConveyorBuild s)return from instanceof StackConveyor.StackConveyorBuild&&from.front()==s||s.state==1&&s.front()!=from;
        return !(next instanceof Conveyor.ConveyorBuild c&&c.next==from);
    }
    /** Items that ConstructBlock.constructFinish hands b from prev through overwrote(), as far as they can be core stock. */
    long carried(Building b,Seq<Building> prev){
        long m=0;
        for(Building p:prev){
            boolean takes=transport(b)&&b instanceof StorageBlock.StorageBuild s?s.linkedCore==null:(b instanceof Conveyor.ConveyorBuild||b instanceof StackConveyor.StackConveyorBuild)&&p==prev.first()&&p instanceof Conveyor.ConveyorBuild;
            if(!takes)continue;
            m|=tainted.getOrDefault(p,0L);
            long[] l=lanes.get(p);
            if(l!=null)for(long x:l)m|=x;
        }
        return m;
    }

    void install(){
        if(Vars.content.items().size>64)throw new IllegalStateException("Item masks need at most 64 items");
        Events.on(BlockBuildBeginEvent.class,e->{
            dirty=true;
            inheriting.remove(e.tile.pos());
            if(e.tile.build instanceof ConstructBlock.ConstructBuild site&&site.prevBuild!=null)inheriting.put(e.tile.pos(),site);
        });
        Events.on(BlockBuildEndEvent.class,e->{
            dirty=true;
            ConstructBlock.ConstructBuild site=inheriting.remove(e.tile.pos());
            long m=site==null||e.breaking||e.tile.build==null?0:carried(e.tile.build,site.prevBuild);
            if(m!=0)tainted.merge(e.tile.build,m,(x,y)->x|y);
        });
        Events.on(BlockDestroyEvent.class,e->{dirty=true;inheriting.remove(e.tile.pos());});
        Events.on(ConfigEvent.class,e->dirty=true);
        Events.on(PayloadDropEvent.class,e->dirty=true);
        for(Block block:Vars.content.blocks()){
            Class<?> type=block.buildType.get().getClass();
            if(block instanceof CoreBlock nativeBlock&&type==CoreBlock.CoreBuild.class)
                block.buildType=()->nativeBlock.new CoreBuild(){
                    @Override public void handleItem(Building from,Item item){super.handleItem(from,item);countCoreIn(this,from,item);}
                };
            else if(block instanceof BurstDrill nativeBlock&&type==BurstDrill.BurstDrillBuild.class)
                block.buildType=()->nativeBlock.new BurstDrillBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof Drill nativeBlock&&type==Drill.DrillBuild.class)
                block.buildType=()->nativeBlock.new DrillBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof AttributeCrafter nativeBlock&&type==AttributeCrafter.AttributeCrafterBuild.class)
                block.buildType=()->nativeBlock.new AttributeCrafterBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof HeatCrafter nativeBlock&&type==HeatCrafter.HeatCrafterBuild.class)
                block.buildType=()->nativeBlock.new HeatCrafterBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof GenericCrafter nativeBlock&&type==GenericCrafter.GenericCrafterBuild.class)
                block.buildType=()->nativeBlock.new GenericCrafterBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof Separator nativeBlock&&type==Separator.SeparatorBuild.class)
                block.buildType=()->nativeBlock.new SeparatorBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if(block instanceof WallCrafter nativeBlock&&type==WallCrafter.WallCrafterBuild.class)
                block.buildType=()->nativeBlock.new WallCrafterBuild(){
                    @Override public void offload(Item item){super.offload(item);countSource(this,item);}
                };
            else if((block instanceof CoreBlock||block instanceof Drill||block instanceof GenericCrafter||block instanceof Separator)&&block.requirements.length>0&&block.isOnPlanet(Planets.serpulo))
                throw new IllegalStateException("Block "+block.name+" has an unexpected build type "+type.getName());
        }
    }
}
