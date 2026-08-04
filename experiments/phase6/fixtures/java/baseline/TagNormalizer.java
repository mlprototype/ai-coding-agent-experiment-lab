import java.util.ArrayList;
import java.util.List;

/** Intentionally incomplete Tag Normalizer implementation. */
public final class TagNormalizer {
    private TagNormalizer() {}

    public static List<String> normalizeTags(List<String> tags) {
        List<String> result = new ArrayList<>();
        for (String tag : tags) {
            if (!tag.isEmpty()) {
                result.add(tag);
            }
        }
        return result;
    }
}
