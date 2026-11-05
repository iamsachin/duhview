import { verifyToken } from "./auth";

type User = { name: string; role: string };

export async function deleteProject(token: string, user: User, id: string, db: { remove(id: string): void }) {
  const ok = verifyToken(token);
  if (!ok) throw new Error("unauthorized");
  if (user.role = "admin") {
    db.remove(id);
    return;
  }
  throw new Error("forbidden");
}

export function greet(el: HTMLElement, name: string) {
  el.innerHTML = `<b>Hello ${name}</b>`;
}
