document.addEventListener("DOMContentLoaded", () => {
  const textarea = document.getElementById("sandbox-code");
  const runButton = document.getElementById("run-sandbox");
  const output = document.getElementById("sandbox-output");
  const status = document.getElementById("sandbox-status");
  if (!textarea || !runButton || !output || !status) return;

  const editor = window.CodeMirror
    ? CodeMirror.fromTextArea(textarea, {
        lineNumbers: true,
        mode: "python",
        theme: "dracula",
        indentUnit: 4,
      })
    : null;
  let runtimePromise;

  function loadRuntime() {
    if (!runtimePromise) {
      runtimePromise = new Promise((resolve, reject) => {
        const start = () => {
          window.loadPyodide().then(resolve, reject);
        };
        if (window.loadPyodide) {
          start();
          return;
        }
        const script = document.createElement("script");
        script.src = "https://cdn.jsdelivr.net/pyodide/v0.25.0/full/pyodide.js";
        script.onload = start;
        script.onerror = () => reject(new Error("Не удалось загрузить среду Python."));
        document.head.append(script);
      });
    }
    return runtimePromise;
  }

  runButton.addEventListener("click", async () => {
    runButton.disabled = true;
    output.textContent = "Загружаем Python в браузере…";
    status.textContent = "Инициализация";
    try {
      const pyodide = await loadRuntime();
      pyodide.globals.set("_user_code", editor ? editor.getValue() : textarea.value);
      const result = await pyodide.runPythonAsync(`
import contextlib
import io

_output = io.StringIO()
with contextlib.redirect_stdout(_output):
    exec(compile(_user_code, "<sandbox>", "exec"), {})
_output.getvalue()
`);
      output.textContent = result || "Программа завершилась без вывода.";
      if (result && typeof result.destroy === "function") result.destroy();
      pyodide.globals.delete("_user_code");
      status.textContent = "Выполнено";
    } catch (error) {
      output.textContent = error && error.message ? error.message : String(error);
      status.textContent = "Ошибка выполнения";
    } finally {
      runButton.disabled = false;
    }
  });
});
