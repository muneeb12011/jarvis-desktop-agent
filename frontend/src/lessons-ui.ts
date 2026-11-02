/**
 * Settings > What Jarvis has learned: the standing instructions and corrections Jarvis keeps about how you
 * like things done, each one removable. The backend owns the list (learning.py); this file only shows it and
 * sends `lesson_delete`.
 *
 * Like brains.ts it is a plain script: no top-level import/export, and every top-level name starts with
 * `lesson` so nothing collides with renderer.ts.
 */

interface LessonView {
  id: number;
  text: string;
  kind: string;
  uses: number;
  standing: boolean;
}

/** Called by the renderer with the backend's list (on connect, and whenever it changes). */
function renderLessons(items?: LessonView[]) {
  const list = document.getElementById("lessonList");
  const empty = document.getElementById("lessonEmpty");
  if (!list || !items) return;
  list.replaceChildren(
    ...items.map((item) => {
      const li = document.createElement("li");
      li.className = "lesson";
      li.dataset.id = String(item.id);

      const text = document.createElement("span");
      text.className = "lessonText";
      text.textContent = item.text;

      const meta = document.createElement("span");
      meta.className = "lessonMeta";
      const bits = [item.kind === "correction" ? "from a correction" : "you said"];
      if (item.uses > 0) bits.push(`used ${item.uses}×`);
      meta.textContent = bits.join(" · ");

      const forget = document.createElement("button");
      forget.type = "button";
      forget.className = "lessonForget";
      forget.textContent = "×";
      forget.title = "Forget this";
      forget.setAttribute("aria-label", `Forget: ${item.text}`);

      const body = document.createElement("div");
      body.className = "lessonBody";
      body.append(text, meta);
      li.append(body, forget);
      return li;
    }),
  );
  if (empty) empty.hidden = items.length > 0;
}

document.getElementById("lessonList")?.addEventListener("click", (e) => {
  const button = (e.target as HTMLElement).closest("button.lessonForget");
  const li = button?.closest("li.lesson") as HTMLLIElement | null;
  if (li?.dataset.id) hudAction("lesson_delete", { id: Number(li.dataset.id) });
});
