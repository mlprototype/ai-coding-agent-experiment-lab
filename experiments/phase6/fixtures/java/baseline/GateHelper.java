import java.io.IOException;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

/** Dependency-free deterministic Gates for the Java Fixture. */
public final class GateHelper {
    private GateHelper() {}

    private static Path compile() throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            return null;
        }
        Path output = Path.of(
            System.getenv("TMPDIR"),
            "phase6-java-" + ProcessHandle.current().pid()
        );
        Files.createDirectories(output);
        int result = compiler.run(
            null,
            null,
            null,
            "-d",
            output.toString(),
            "TagNormalizer.java"
        );
        return result == 0 ? output : null;
    }

    @SuppressWarnings("unchecked")
    private static List<String> normalize(List<String> input) throws Exception {
        Path output = compile();
        if (output == null) {
            return null;
        }
        try (URLClassLoader loader = new URLClassLoader(new URL[] {output.toUri().toURL()})) {
            Class<?> implementation = loader.loadClass("TagNormalizer");
            Method method = implementation.getMethod("normalizeTags", List.class);
            return (List<String>) method.invoke(null, input);
        }
    }

    private static boolean acceptance() throws Exception {
        List<String> actual = normalize(List.of(
            "  Hello World  ",
            "hello__world",
            "Alpha_beta",
            " --Trim-- ",
            "___",
            "ALPHA BETA",
            "x   y"
        ));
        return List.of("hello-world", "alpha-beta", "trim", "x-y").equals(actual);
    }

    private static boolean regression() throws Exception {
        return List.of().equals(normalize(List.of()))
            && List.of("plain-tag").equals(normalize(List.of("plain-tag")));
    }

    private static boolean lint() throws IOException {
        String content = Files.readString(Path.of("TagNormalizer.java"), StandardCharsets.UTF_8);
        return content.contains("public static List<String> normalizeTags")
            && !content.contains("\t")
            && content.lines().allMatch(line -> line.equals(line.stripTrailing()));
    }

    private static boolean typecheck() throws IOException {
        return compile() != null;
    }

    public static void main(String[] arguments) throws Exception {
        if (arguments.length != 1) {
            System.exit(2);
        }
        boolean passed = switch (arguments[0]) {
            case "acceptance" -> acceptance();
            case "regression" -> regression();
            case "lint" -> lint();
            case "typecheck" -> typecheck();
            default -> false;
        };
        System.exit(passed ? 0 : 1);
    }
}
