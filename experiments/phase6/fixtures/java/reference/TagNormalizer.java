import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Reference Tag Normalizer implementation, isolated from Provider workspaces. */
public final class TagNormalizer {
    private TagNormalizer() {}

    private static boolean separator(char value) {
        return value == ' ' || value == '\t' || value == '\r' || value == '\n'
            || value == '\f' || value == '\u000b' || value == '_';
    }

    private static String normalize(String raw) {
        StringBuilder value = new StringBuilder();
        boolean pendingSeparator = false;
        for (int index = 0; index < raw.length(); index++) {
            char character = raw.charAt(index);
            if (separator(character)) {
                pendingSeparator = value.length() > 0;
                continue;
            }
            if (character == '-' && value.length() == 0) {
                continue;
            }
            if (pendingSeparator) {
                value.append('-');
            }
            pendingSeparator = false;
            if (character >= 'A' && character <= 'Z') {
                character = (char) (character + ('a' - 'A'));
            }
            value.append(character);
        }
        while (!value.isEmpty() && value.charAt(value.length() - 1) == '-') {
            value.deleteCharAt(value.length() - 1);
        }
        return value.toString();
    }

    public static List<String> normalizeTags(List<String> tags) {
        List<String> normalized = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (String raw : tags) {
            String value = normalize(raw);
            if (!value.isEmpty() && seen.add(value)) {
                normalized.add(value);
            }
        }
        return normalized;
    }
}
