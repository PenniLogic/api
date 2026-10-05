import org.pitest.mutationtest.config.PluginServices;
import org.pitest.mutationtest.engine.gregor.config.Mutator;

class PitCatalogue {
    public static void main(String[] args) {
        for (var mutator : Mutator.all()) {
            System.out.println("MUTATOR\t" + mutator.getName() + "\t" + mutator.getGloballyUniqueId());
        }
        for (var factory : PluginServices.makeForContextLoader().findInterceptors()) {
            var feature = factory.provides();
            System.out.println("FEATURE\t" + feature.name() + "\t" + feature.isOnByDefault());
        }
    }
}
