document.addEventListener("DOMContentLoaded", () => {
  const resultDiv = document.getElementById("result");
  const hintBox = document.getElementById("hint-box");
  const nextBtn = document.getElementById("next-btn");

  // --- ЛЕКЦИЯ ---
  const theoryBtn = document.getElementById("complete-theory-btn");
  if (theoryBtn) {
    theoryBtn.addEventListener("click", async () => {
      theoryBtn.disabled = true;
      await submitStep(theoryBtn.dataset.slug, "lecture");
    });
  }

  // --- ПРАКТИКА ---
  const practiceBtn = document.getElementById("check-practice-btn");
  const textarea = document.getElementById("code-editor");
  let editor;

  if (practiceBtn && textarea) {
    editor = CodeMirror.fromTextArea(textarea, {
      lineNumbers: true, mode: "python", theme: "dracula", indentUnit: 4
    });

    practiceBtn.addEventListener("click", async () => {
      const answer = editor.getValue();
      if (!answer.trim()) {
        resultDiv.innerHTML = '<span style="color: #dc3545;">Введите ответ!</span>';
        return;
      }
      practiceBtn.disabled = true;
      practiceBtn.innerText = "Проверка...";
      await submitStep(practiceBtn.dataset.slug, answer);
    });
  }

  // --- ТЕСТ ---
  const quizBtn = document.getElementById("check-quiz-btn");
  if (quizBtn) {
    quizBtn.addEventListener("click", async () => {
      const form = document.getElementById("quiz-form");
      const selected = form.querySelector('input[name="quiz"]:checked');
      if (!selected) {
        resultDiv.innerHTML = '<span style="color: #dc3545;">Выберите вариант!</span>';
        return;
      }
      quizBtn.disabled = true;
      quizBtn.innerText = "Проверка...";
      await submitStep(quizBtn.dataset.slug, selected.value);
    });
  }

  // --- ОБЩАЯ ФУНКЦИЯ ---
  async function submitStep(slug, answer) {
    resultDiv.innerHTML = "";
    
    try {
      const res = await fetch("/api/submit_step", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: slug, answer: answer }),
      });
      const data = await res.json();

      if (data.ok) {
        resultDiv.innerHTML = `<span style="color: #198754;">✔ Верно! +${data.xp} XP</span>`;
        if (nextBtn) nextBtn.style.display = "inline-block";
      } else {
        resultDiv.textContent = data.error || "✘ Ответ не принят. Проверьте решение и попробуйте ещё раз.";
        resultDiv.style.color = "#dc3545";
        if (practiceBtn) { practiceBtn.disabled = false; practiceBtn.innerText = "Отправить"; }
        if (quizBtn) { quizBtn.disabled = false; quizBtn.innerText = "Ответить"; }
        if (theoryBtn) { theoryBtn.disabled = false; }
      }

      const counter = document.getElementById("attempts-counter");
      if (counter && data.attempts !== undefined) {
        counter.innerText = "Попыток: " + data.attempts;
      }

      if (data.hint) {
        hintBox.style.display = "block";
        hintBox.textContent = `💡 Подсказка: ${data.hint}`;
      }

    } catch (e) {
      resultDiv.textContent = "Не удалось связаться с сервером. Проверьте подключение и повторите попытку.";
      resultDiv.style.color = "#dc3545";
    }
  }

  const commentButton = document.getElementById("submit-comment-btn");
  if (commentButton) {
    commentButton.addEventListener("click", async () => {
      const input = document.getElementById("comment-input");
      const error = document.getElementById("comment-error");
      const commentsList = document.getElementById("comments-list");
      const text = input.value.trim();
      error.textContent = "";
      if (!text) {
        error.textContent = "Введите текст комментария.";
        return;
      }

      commentButton.disabled = true;
      try {
        const response = await fetch("/api/add_comment", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lesson_id: commentButton.dataset.lessonId, text }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          error.textContent = data.error || "Не удалось отправить комментарий.";
          return;
        }

        const emptyMessage = commentsList.querySelector(".text-muted");
        if (emptyMessage && emptyMessage.parentElement === commentsList) emptyMessage.remove();
        const article = document.createElement("article");
        article.className = "comment";
        const header = document.createElement("div");
        header.className = "comment-header";
        const username = document.createElement("b");
        username.textContent = data.comment.username;
        const createdAt = document.createElement("span");
        createdAt.className = "text-muted";
        createdAt.style.fontSize = "12px";
        createdAt.textContent = data.comment.created_at.slice(0, 16);
        const body = document.createElement("p");
        body.style.margin = "5px 0 0";
        body.textContent = data.comment.text;
        header.append(username, createdAt);
        article.append(header, body);
        commentsList.prepend(article);
        input.value = "";
      } catch (e) {
        error.textContent = "Не удалось связаться с сервером. Повторите попытку.";
      } finally {
        commentButton.disabled = false;
      }
    });
  }
});