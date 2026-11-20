// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.struct.Seq;
import mindustry.Vars;
import mindustry.content.UnitTypes;
import mindustry.game.SpawnGroup;
import mindustry.type.UnitType;
import mindustry.world.Tile;
import java.util.*;

final class ProgressiveWaves {
    static final double REFERENCE_DAMAGE=30,ENTER_FROM=3,ENTER_SPAN=12,FADE_FROM=40,FADE_SPAN=160,FADE_DEPTH=0.9;
    static final int ROUNDS=12;
    record Kind(UnitType type,int cap){}
    static List<Kind> kinds(){return List.of(
        new Kind(UnitTypes.dagger,20),new Kind(UnitTypes.flare,20),new Kind(UnitTypes.crawler,20),new Kind(UnitTypes.nova,20),
        new Kind(UnitTypes.mace,16),new Kind(UnitTypes.horizon,16),new Kind(UnitTypes.atrax,16),new Kind(UnitTypes.pulsar,16),
        new Kind(UnitTypes.fortress,12),new Kind(UnitTypes.zenith,12),new Kind(UnitTypes.quasar,12),new Kind(UnitTypes.spiroct,12),
        new Kind(UnitTypes.quad,6),new Kind(UnitTypes.arkyid,6),new Kind(UnitTypes.vela,6),new Kind(UnitTypes.antumbra,6),new Kind(UnitTypes.scepter,6),
        new Kind(UnitTypes.toxopid,4),new Kind(UnitTypes.corvus,4),new Kind(UnitTypes.eclipse,4),new Kind(UnitTypes.reign,4));}
    static double armorFactor(double armor){return REFERENCE_DAMAGE/Math.max(REFERENCE_DAMAGE-armor,Vars.minArmorDamage*REFERENCE_DAMAGE);}
    static double strength(UnitType type){return type.health*armorFactor(type.armor);}
    static double budget(int wave,double first,double growth){double b=first;for(int i=1;i<wave;i++)b*=growth;return b;}
    static boolean open(double[] weight,int[] counts,List<Kind> kinds,int i){return weight[i]>0&&counts[i]<kinds.get(i).cap();}
    static double smooth(double x){x=Math.min(1,Math.max(0,x));return x*x*(3-2*x);}

    record Composition(int[] counts,double shield){}
    static Composition composition(int wave,double first,double growth){
        List<Kind> kinds=kinds();int n=kinds.size();
        double budget=budget(wave,first,growth),left=budget;
        double[] weight=new double[n],unit=new double[n],ideal=new double[n];
        int[] counts=new int[n];
        for(int i=0;i<n;i++){
            unit[i]=strength(kinds.get(i).type());
            double x=budget/unit[i];
            weight[i]=(i==0?1:smooth((x-ENTER_FROM)/ENTER_SPAN))*(1-FADE_DEPTH*smooth((x-FADE_FROM)/FADE_SPAN));
        }

        for(int round=0;round<ROUNDS;round++){
            double total=0;for(int i=0;i<n;i++)if(open(weight,counts,kinds,i))total+=weight[i];
            if(total==0)break;
            double start=left;int added=0;
            for(int i=0;i<n;i++)if(open(weight,counts,kinds,i)){
                ideal[i]=Math.min(start*weight[i]/total/unit[i],kinds.get(i).cap()-counts[i]);
                int k=(int)ideal[i];counts[i]+=k;left-=k*unit[i];added+=k;
            }
            if(added==0)break;
        }
        List<Integer> order=new ArrayList<>();for(int i=0;i<n;i++)if(open(weight,counts,kinds,i))order.add(i);
        order.sort(Comparator.<Integer>comparingDouble(i->-(ideal[i]-(int)ideal[i])).thenComparingInt(i->i));
        for(int i:order)if(unit[i]<=left){counts[i]++;left-=unit[i];}
        double armored=0;for(int i=0;i<n;i++)if(counts[i]>0)armored+=counts[i]*armorFactor(kinds.get(i).type().armor);
        return new Composition(counts,left>0&&armored>0?left/armored:0);
    }

    static Seq<SpawnGroup> groups(int wave,Seq<Tile> spawns,double first,double growth){
        Composition c=composition(wave,first,growth);List<Kind> kinds=kinds();Seq<SpawnGroup> out=new Seq<>();
        int s=spawns.size;
        for(int i=0;i<kinds.size();i++){
            int count=c.counts()[i];if(count==0)continue;
            if(count/s>0)out.add(group(kinds.get(i).type(),wave,count/s,-1,c.shield()));
            for(int k=0;k<count%s;k++)out.add(group(kinds.get(i).type(),wave,1,spawns.get((i+wave+k)%s).pos(),c.shield()));
        }
        return out;
    }
    static SpawnGroup group(UnitType type,int wave,int amount,int spawn,double shield){
        SpawnGroup g=new SpawnGroup(type);
        g.begin=g.end=wave-1;g.spacing=1;g.unitAmount=g.max=amount;g.spawn=spawn;g.shields=(float)shield;g.shieldScaling=0;
        return g;
    }
}
