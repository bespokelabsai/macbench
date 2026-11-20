// SPDX-License-Identifier: GPL-3.0-only

package agentbridge;

import arc.util.serialization.JsonValue;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import static agentbridge.AgentBridge.*;

/** Deterministic world layout: plain arrays of Serpulo block names, independent of the engine's global state. */
final class WorldGen {
    /** Profile values the bridge uses. Other keys in the object (grader or host settings) are ignored. */
    record Profile(int mapSize,int sightRadius,int fronts,int spawnRadius,float firstWaveTicks,float waveSpacingTicks,
                   double firstBudget,double growth,Map<String,Integer> loadout){
        static Profile parse(JsonValue v){
            require(v!=null&&v.isObject(),"profile must be an object");
            Map<String,Integer> loadout=new LinkedHashMap<>();
            JsonValue items=v.get("loadout");require(items!=null&&items.isObject(),"profile.loadout must be an object");
            for(JsonValue item:items){require(Records.integer(item)&&item.asInt()>=0,"profile.loadout."+item.name+" must be a nonnegative integer");loadout.put(item.name,item.asInt());}
            Profile p=new Profile((int)number(v,"map_size",100,1000),(int)number(v,"sight_radius_tiles",1,1000),(int)number(v,"fronts",1,64),(int)number(v,"spawn_radius",10,500),
                (float)number(v,"first_wave_ticks",1,1e9),(float)number(v,"wave_spacing_ticks",1,1e9),number(v,"first_budget",1,1e12),number(v,"growth",1,100),loadout);
            require(p.spawnRadius+BORDER+SPAWN_CLEAR<=p.mapSize/2,"profile.spawn_radius does not fit inside map_size");
            return p;
        }
        static double number(JsonValue v,String key,double min,double max){
            JsonValue n=v.get(key);require(n!=null&&Records.number(n)&&n.asDouble()>=min&&n.asDouble()<=max,"profile."+key+" must be a number in "+min+".."+max);
            return n.asDouble();
        }
    }
    record Field(String kind,int x,int y){}
    record Layout(int size,String[] floor,String[] overlay,String[] wall,int coreX,int coreY,List<int[]> spawns,List<Field> fields,int attempts){
        int at(int x,int y){return y*size+x;}
        String hash(){return hash(size,(x,y)->new String[]{floor[at(x,y)],overlay[at(x,y)],wall[at(x,y)]});}
        interface Cell{String[] names(int x,int y);}
        static String hash(int size,Cell cell){
            try{
                MessageDigest digest=MessageDigest.getInstance("SHA-256");
                for(int y=0;y<size;y++)for(int x=0;x<size;x++)digest.update(String.join("|",cell.names(x,y)).concat("\n").getBytes(StandardCharsets.UTF_8));
                StringBuilder out=new StringBuilder();for(byte b:digest.digest())out.append(String.format("%02x",b));
                return out.toString();
            }catch(java.security.NoSuchAlgorithmException e){throw new IllegalStateException(e);}
        }
    }
    static final String STONE="stone",SAND="sand-floor",WALL="stone-wall",AIR="air",SPAWN="spawn";
    static final int BORDER=6,SPAWN_CLEAR=9,CORE_CLEAR=16,MAX_ATTEMPTS=200;
    /** kind, overlay (null: floor field), radius band at spawn_radius 165, fields per kind. */
    record Resource(String kind,String overlay,int inner,int outer,int count){}
    static final List<Resource> RESOURCES=List.of(
        new Resource("copper","ore-copper",40,60,4),new Resource("lead","ore-lead",40,60,4),
        new Resource("sand",null,70,90,4),new Resource("scrap","ore-scrap",80,120,4),
        new Resource("coal","ore-coal",90,120,5),new Resource("titanium","ore-titanium",120,150,5));

    private WorldGen(){}

    static Layout generate(long seed,Profile p){
        for(int attempt=1;attempt<=MAX_ATTEMPTS;attempt++){
            Layout layout=attempt(new Random(seed*0x9E3779B97F4A7C15L+attempt),p,attempt);
            if(allSpawnsReachCore(layout))return layout;
        }
        throw new IllegalStateException("No valid layout in "+MAX_ATTEMPTS+" attempts");
    }

    private static Layout attempt(Random rnd,Profile p,int attempt){
        int n=p.mapSize,c=n/2;
        String[] floor=new String[n*n],overlay=new String[n*n],wall=new String[n*n];
        Arrays.fill(floor,STONE);Arrays.fill(overlay,AIR);Arrays.fill(wall,AIR);
        int blobs=n*n/1800;
        for(int i=0;i<blobs;i++){
            int x=BORDER+rnd.nextInt(n-2*BORDER),y=BORDER+rnd.nextInt(n-2*BORDER);
            for(int k=0;k<3;k++){
                int cx=x+rnd.nextInt(9)-4,cy=y+rnd.nextInt(9)-4;
                disc(n,cx,cy,3+rnd.nextInt(6),t->wall[t]=WALL);
            }
        }
        double scale=p.spawnRadius/165d,copperBase=rnd.nextDouble()*2*Math.PI;
        ArrayList<Field> fields=new ArrayList<>();
        for(int r=0;r<RESOURCES.size();r++){
            Resource res=RESOURCES.get(r);
            // copper and lead interleave so the first ring of ore never lies in one direction.
            double step=2*Math.PI/res.count,base=res.kind.equals("copper")?copperBase:res.kind.equals("lead")?copperBase+step/2:rnd.nextDouble()*2*Math.PI;
            for(int i=0;i<res.count;i++){
                double angle=base+i*step+(rnd.nextDouble()-0.5)*step*0.3,radius=(res.inner+rnd.nextDouble()*(res.outer-res.inner))*scale;
                int fx=c+(int)Math.round(Math.cos(angle)*radius),fy=c+(int)Math.round(Math.sin(angle)*radius);
                fields.add(new Field(res.kind,fx,fy));
                disc(n,fx,fy,12,t->wall[t]=AIR);
                for(int k=0;k<4;k++){
                    int cx=fx+rnd.nextInt(9)-4,cy=fy+rnd.nextInt(9)-4,size=4+rnd.nextInt(4);
                    disc(n,cx,cy,size,t->{
                        if(res.overlay==null)floor[t]=SAND;
                        else if(rnd.nextDouble()<0.9)overlay[t]=res.overlay;
                    });
                }
            }
        }
        disc(n,c,c,CORE_CLEAR,t->{wall[t]=AIR;overlay[t]=AIR;floor[t]=STONE;});
        ArrayList<int[]> spawns=new ArrayList<>();
        double offset=rnd.nextDouble()*2*Math.PI;
        for(int i=0;i<p.fronts;i++){
            double angle=offset+i*2*Math.PI/p.fronts;
            int sx=c+(int)Math.round(Math.cos(angle)*p.spawnRadius),sy=c+(int)Math.round(Math.sin(angle)*p.spawnRadius);
            disc(n,sx,sy,SPAWN_CLEAR,t->{wall[t]=AIR;overlay[t]=AIR;});
            overlay[sy*n+sx]=SPAWN;
            spawns.add(new int[]{sx,sy});
        }
        for(int y=0;y<n;y++)for(int x=0;x<n;x++)if(x<BORDER||y<BORDER||x>=n-BORDER||y>=n-BORDER){wall[y*n+x]=WALL;overlay[y*n+x]=AIR;floor[y*n+x]=STONE;}
        for(int t=0;t<n*n;t++)if(wall[t].equals(WALL))overlay[t]=AIR;
        return new Layout(n,floor,overlay,wall,c,c,spawns,fields,attempt);
    }

    private interface TileAction{void at(int index);}
    private static void disc(int n,int cx,int cy,int r,TileAction action){
        for(int y=Math.max(0,cy-r);y<=Math.min(n-1,cy+r);y++)for(int x=Math.max(0,cx-r);x<=Math.min(n-1,cx+r);x++)
            if((x-cx)*(x-cx)+(y-cy)*(y-cy)<=r*r)action.at(y*n+x);
    }

    /** Breadth-first search from the core over tiles whose 3x3 neighbourhood is free, so wide ground units fit too. */
    static boolean allSpawnsReachCore(Layout l){
        int n=l.size;boolean[] seen=new boolean[n*n];ArrayDeque<Integer> queue=new ArrayDeque<>();
        int start=l.at(l.coreX+3,l.coreY);seen[start]=true;queue.add(start);
        int[] dx={1,-1,0,0},dy={0,0,1,-1};
        while(!queue.isEmpty()){
            int t=queue.poll(),x=t%n,y=t/n;
            for(int d=0;d<4;d++){
                int nx=x+dx[d],ny=y+dy[d];
                if(nx<1||ny<1||nx>=n-1||ny>=n-1||seen[ny*n+nx]||!clear(l,nx,ny))continue;
                seen[ny*n+nx]=true;queue.add(ny*n+nx);
            }
        }
        for(int[] s:l.spawns)if(!seen[l.at(s[0],s[1])])return false;
        return true;
    }
    private static boolean clear(Layout l,int x,int y){
        for(int dy=-1;dy<=1;dy++)for(int dx=-1;dx<=1;dx++)if(!l.wall[l.at(x+dx,y+dy)].equals(AIR))return false;
        return true;
    }
}
