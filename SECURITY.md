# Security Policy

## Supported versions

native2py is pre-1.0 and ships from `main`. Only the latest commit on `main`
receives security fixes; there are no maintained release branches yet.

## Reporting a vulnerability

> **TODO (repo owner): replace the placeholder below with a real contact.**
> Until then there is no working private reporting channel. The preferred fix
> is to enable GitHub [private vulnerability reporting][pvr] on this repository
> (Settings → Security → Reporting), which needs no email address at all.

**Contact: `SECURITY-CONTACT-NOT-YET-SET` — placeholder, not a real address.**

Please do **not** open a public issue for a suspected vulnerability. Once a
channel exists, report privately and include:

- what the issue is and why you believe it is a security problem,
- the affected file, command, or generated artifact,
- a minimal reproduction (input header/source plus the `native2py` command),
- the version or commit SHA you tested.

Expect an acknowledgement within a few days. This is a small project with no
paid on-call; there is no bug bounty.

[pvr]: https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability

## Threat model — what native2py actually does

Be aware of the trust boundaries before reporting, and before using the tool:

- **native2py parses and generates code, it does not sandbox it.** Pointing it
  at a C++ or Fortran source tree means parsing that tree (with libclang) and
  emitting binding code derived from it. Treat input sources as trusted, the
  same way you would treat any code you are about to compile.
- **Generated services execute native code in-process.** A wrapped library's
  memory-safety bugs become the service's bugs. Generated FastAPI services
  carry no authentication, and the generated Dockerfile runs uvicorn on
  `0.0.0.0:8000` — do not publish that port to an untrusted network without
  putting your own authn/authz in front of it.
- **Building requires a real toolchain.** The build path invokes cmake, ninja,
  a C++ compiler, and (for Fortran) gfortran, all with the privileges of the
  invoking user.

Issues genuinely in scope include: native2py emitting bindings that misuse a
buffer or lifetime in a way the source did not imply, config or path handling
that escapes the project directory, and template injection through project
metadata into generated code.
