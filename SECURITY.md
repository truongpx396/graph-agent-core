# Security policy

## Reporting a vulnerability

Please report it privately: **Security** tab of this repository -> **Report a vulnerability** (GitHub private vulnerability reporting). Do not open a
public issue or pull request for something that can be exploited.

Say what you did, what happened, and what you expected, with the commit you tested. This is a one-maintainer project: expect an acknowledgement
within a week, and a fix or a plan after the report is confirmed. There is no bounty.

## Supported versions

Only the `main` branch. There are no releases, and no fix is back-ported.

## What is checked automatically

Every change and once a week: Semgrep (source), Trivy (dependencies, secrets, Dockerfiles, built images), Checkov (Terraform) and govulncheck (the Go
service). GitHub secret scanning with push protection and Dependabot alerts are on. Only fixable **critical** findings block a merge; the rest are
listed in the Security tab. See [the scanners](README.md#-security-scanning-and-load-testing) for how to run each one.

## Testing

Test only a deployment you run yourself. The pentest tools in this repository (`make strix`, `make zap-api-scan`) attack their target.
