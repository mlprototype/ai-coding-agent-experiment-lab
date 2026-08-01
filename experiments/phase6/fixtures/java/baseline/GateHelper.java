import java.io.IOException;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/** Dependency-free deterministic Gates for the Java Fixture. */
public final class GateHelper {
    private GateHelper() {}

    private static Path compile(String javac) throws IOException, InterruptedException {
        Path output = Path.of(
            System.getenv("TMPDIR"),
            "phase6-java-" + ProcessHandle.current().pid()
        );
        Files.createDirectories(output);
        Process compiler = new ProcessBuilder(
            javac,
            "-d",
            output.toString(),
            "TagNormalizer.java"
        ).inheritIO().start();
        return compiler.waitFor() == 0 ? output : null;
    }

    @SuppressWarnings("unchecked")
    private static List<String> normalize(List<String> input, String javac) throws Exception {
        Path output = compile(javac);
        if (output == null) {
            return null;
        }
        try (URLClassLoader loader = new URLClassLoader(new URL[] {output.toUri().toURL()})) {
            Class<?> implementation = loader.loadClass("TagNormalizer");
            Method method = implementation.getMethod("normalizeTags", List.class);
            return (List<String>) method.invoke(null, input);
        }
    }

    private static boolean acceptance(String javac) throws Exception {
        List<String> actual = normalize(List.of(
            "  Hello World  ",
            "hello__world",
            "Alpha_beta",
            " --Trim-- ",
            "___",
            "ALPHA BETA",
            "x   y",
            "a- _b"
        ), javac);
        return List.of("hello-world", "alpha-beta", "trim", "x-y", "a--b")
            .equals(actual);
    }

    private static boolean regression(String javac) throws Exception {
        return List.of().equals(normalize(List.of(), javac))
            && List.of("plain-tag").equals(normalize(List.of("plain-tag"), javac));
    }

    private static boolean lint() throws IOException {
        String content = Files.readString(Path.of("TagNormalizer.java"), StandardCharsets.UTF_8);
        return content.contains("public static List<String> normalizeTags")
            && !content.contains("\t")
            && content.lines().allMatch(line -> line.equals(line.stripTrailing()));
    }

    private static boolean typecheck(String javac) throws IOException, InterruptedException {
        return compile(javac) != null;
    }

    public static void main(String[] arguments) throws Exception {
        if (arguments.length != 2) {
            System.exit(2);
        }
        String javac = arguments[1];
        boolean passed = switch (arguments[0]) {
            case "acceptance" -> acceptance(javac);
            case "regression" -> regression(javac);
            case "lint" -> lint();
            case "typecheck" -> typecheck(javac);
            default -> false;
        };
        System.exit(passed ? 0 : 1);
    }
}
