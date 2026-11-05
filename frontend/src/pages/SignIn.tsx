import { useState, type FormEvent } from "react";
import { useAuth } from "../lib/auth";
import { Button } from "../components/ui/Button";

export function SignIn() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  const field =
    "mt-1 w-full border border-rule-strong bg-paper px-3 py-2.5 text-[1rem]";
  return (
    <div className="grid min-h-screen grid-cols-1 md:grid-cols-[1.1fr_1fr]">
      <div className="hidden flex-col justify-between border-r border-rule bg-wash p-14 md:flex">
        <div>
          <div className="text-[1.6rem] leading-none">Recon</div>
          <div className="eyebrow mt-1.5">Isolabs finance</div>
        </div>
        <div className="max-w-[30ch]">
          <p className="text-[2.1rem] leading-[1.2]">
            Every rupee a partner owes you, found and evidenced.
          </p>
          <p className="mt-4 text-muted">
            Upload settlement, COD and freight files. See per order whether you
            were paid the right amount, and what to claim before the window
            closes.
          </p>
        </div>
        <p className="text-[0.85rem] text-faint">
          Customer personal data is masked wherever it is not needed.
        </p>
      </div>
      <div className="flex items-center justify-center p-10">
        <form
          onSubmit={submit}
          className="w-full max-w-[22rem]"
          aria-label="Sign in"
        >
          <h1 className="text-[1.8rem]">Sign in</h1>
          <label className="mt-7 block text-[0.95rem]">
            Email
            <input
              className={field}
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="mt-4 block text-[0.95rem]">
            Password
            <input
              className={field}
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p
              role="alert"
              className="mt-4 border-l-4 border-l-risk pl-3 text-[0.95rem] text-risk"
            >
              {error}
            </p>
          )}
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            className="mt-6 w-full"
          >
            Sign in
          </Button>
        </form>
      </div>
    </div>
  );
}
