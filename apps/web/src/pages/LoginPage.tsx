import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowRight, CircuitBoard, KeyRound, LockKeyhole, ShieldCheck, Wifi } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";
import { apiBase, errorMessage, generatedApi } from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { Button, Field } from "../components/ui";

const schema = z.object({
  organisation_slug: z.string().max(100).optional(),
  username: z.string().min(1, "Enter your username").max(100),
  password: z.string().min(1, "Enter your password").max(4096),
});
type LoginFields = z.infer<typeof schema>;

function safeDestination(value: string | null | undefined): string {
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export function LoginPage() {
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const [submitError, setSubmitError] = useState<string>();
  const destination = safeDestination(
    search.get("returnTo") ?? (location.state as { from?: string } | null)?.from,
  );
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginFields>({ resolver: zodResolver(schema) });
  if (auth.authenticated) return <Navigate to={destination} replace />;

  async function onSubmit(fields: LoginFields) {
    setSubmitError(undefined);
    try {
      await generatedApi.login({
        ...fields,
        organisation_slug: fields.organisation_slug || null,
        browser: true,
      });
      await auth.refresh();
      navigate(destination, { replace: true });
    } catch (error) {
      setSubmitError(errorMessage(error));
    }
  }

  const oidcUrl = `${apiBase}/api/v1/auth/oidc/login?browser=true&return_to=${encodeURIComponent(destination)}`;
  return (
    <main className="login-page">
      <section className="login-visual" aria-label="Lab Platform introduction">
        <div className="login-brand">
          <span className="brand-mark">
            <CircuitBoard size={22} />
          </span>
          <strong>Lab Platform</strong>
        </div>
        <div className="login-hero">
          <p className="eyebrow">Distributed hardware operations</p>
          <h1>
            Your lab,
            <br />
            under control.
          </h1>
          <p>
            Reserve benches, run workflows, and investigate failures across every Agent from one
            operational view.
          </p>
          <div className="login-trust">
            <span>
              <Wifi size={16} /> Live state
            </span>
            <span>
              <ShieldCheck size={16} /> Permission aware
            </span>
            <span>
              <LockKeyhole size={16} /> Audited actions
            </span>
          </div>
        </div>
        <div className="login-signal" aria-hidden>
          <span />
          <span />
          <span />
          <span />
          <span />
        </div>
      </section>
      <section className="login-form-side">
        <div className="login-card">
          <div className="login-card-heading">
            <div className="login-icon">
              <KeyRound size={22} />
            </div>
            <h2>Welcome back</h2>
            <p>Sign in to your organisation workspace.</p>
          </div>
          {auth.localEnabled && (
            <form onSubmit={handleSubmit(onSubmit)} noValidate>
              <Field
                label="Organisation"
                error={errors.organisation_slug?.message}
                hint="Leave blank to use the default organisation"
              >
                <input
                  autoComplete="organization"
                  placeholder="simlab-demo"
                  {...register("organisation_slug")}
                />
              </Field>
              <Field label="Username" error={errors.username?.message} required>
                <input
                  autoComplete="username"
                  autoFocus
                  placeholder="michael"
                  {...register("username")}
                />
              </Field>
              <Field label="Password" error={errors.password?.message} required>
                <input type="password" autoComplete="current-password" {...register("password")} />
              </Field>
              {submitError && (
                <div className="form-alert" role="alert">
                  {submitError}
                </div>
              )}
              <Button type="submit" className="login-submit" disabled={isSubmitting}>
                {isSubmitting ? (
                  "Signing in…"
                ) : (
                  <>
                    Sign in <ArrowRight size={16} />
                  </>
                )}
              </Button>
            </form>
          )}
          {auth.oidcEnabled && (
            <>
              <div className="or-divider">
                <span>or</span>
              </div>
              <a className="button button-secondary oidc-button" href={oidcUrl}>
                Continue with organisation login
              </a>
            </>
          )}
          {!auth.localEnabled && !auth.oidcEnabled && (
            <div className="form-alert">
              No browser login method is configured. Contact your administrator.
            </div>
          )}
          <p className="login-footnote">Session credentials stay in secure browser cookies.</p>
        </div>
      </section>
    </main>
  );
}
