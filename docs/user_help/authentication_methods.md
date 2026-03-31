---
id: authentication_methods
title: Available login methods
category: auth
keywords:
  - login
  - sign in
  - password
  - magic link
  - Google
  - OAuth
  - authentication
  - register
  - iniciar sesion
  - contrasena
  - enlace magico
  - autenticacion
  - registro
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

Aurvek supports three ways to log in: username and password, magic links (one-time login URLs), and Google Sign-In. Which methods are available to you depends on how your account was configured by the administrator.

## Authentication modes

Your account uses one of these modes, set by your administrator:

- **Magic link only** -- you receive a unique URL that logs you in directly. No password is needed.
- **Password only** -- you log in with your username and password.
- **Magic link + password** -- both methods are available. You can use either one.

If your instance has **Google OAuth** enabled, a "Sign in with Google" button also appears on the login page regardless of your authentication mode.

## Steps

### Logging in with a password

1. Go to the login page.
2. Enter your **username** and **password**.
3. Click **Log In**.

### Logging in with a magic link

1. You will receive a magic link URL (via email or from your administrator).
2. Click the link or paste it into your browser. You are logged in automatically.
3. Magic links expire after 3 days. If yours has expired, go to the **Magic Link Recovery** page (`/magic-link-recovery`), enter your email, and a new link will be sent to you.

### Logging in with Google

1. On the login page, click **Sign in with Google**.
2. Select your Google account and authorize Aurvek.
3. If your Google email matches an existing Aurvek account, your Google account is linked and you are logged in.
4. If no matching account exists, a new account is created for you automatically.

### Registering a new account

1. On the login page, click the **Register** link.
2. Fill in your email and other required fields, then submit.
3. If Google OAuth is available, you can also register by clicking "Sign in with Google" on the registration page.

## Notes

- Sessions last up to 30 days before you need to log in again.
- If you registered via Google and want to set a password for direct login, you will be prompted to do so after your first Google sign-in.
- If your account has password changing enabled, you can update your password from **Settings > Profile > Change Password**.
- CAPTCHA protection (Cloudflare Turnstile or Google reCAPTCHA) may be active on the login page, depending on your instance's configuration.
- The administrator controls which authentication mode your account uses. If you need a different method, contact your administrator.

## Related

- settings_profile
