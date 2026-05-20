# Phase B — Ollama on EKS

> Phase A: every engineer runs Ollama on their laptop. Phase B: the
> team runs one shared Ollama on EKS so a half-dozen engineers don't
> each have to babysit a local model.
>
> Phase A remains the OSS default in v0.8. Phase B is opt-in.

## When to graduate

Phase A is right while vigil is novel: it has no shared
infrastructure to operate, every engineer's brain stays sovereign on
their laptop, and `vigil ask` works offline. The downsides only
surface as the team grows:

- Onboarding requires a local Ollama install, which trips up new
  hires on under-powered laptops.
- Different laptops drift to different model versions and answer
  the same prompt slightly differently.
- The team-shared embedding index needs a stable vector dimension,
  but two engineers on different `nomic-embed-text` versions can
  produce mismatched embeddings.

Graduate to Phase B when **at least two of those bite**. Don't
graduate just because you can.

## What Phase B is, and what it isn't

**Is:** a single in-cluster Ollama instance, fronted by a
`ClusterIP` Service, with model weights persisted on a gp3 PVC. One
endpoint per team, accessed via in-cluster DNS or a port-forward
from a laptop.

**Isn't:** a multi-tenant inference platform, a GPU farm, a custom
operator, or anything that requires you to learn new abstractions.
The whole module is a Deployment + Service + PVC + Job — boring
infra you can read in five minutes.

## Why EKS, not a single EC2

A vigil-shared Ollama on a single EC2 instance would be cheaper
and simpler. We chose EKS anyway:

1. **Reuse what's already running.** Most teams that adopt vigil
   already have an EKS cluster. Adding one Deployment is cheaper
   operationally than spinning up an EC2, attaching a volume,
   patching it, and wiring up monitoring.
2. **ArgoCD parity.** If your platform is GitOps-managed, the same
   ArgoCD controller that reconciles your other workloads also
   reconciles Ollama. No new tool surface.
3. **HA without ceremony.** ReplicaSets, rolling restarts, PVC
   detach/reattach across nodes — all the boring HA features come
   for free.
4. **The ops surface is familiar.** `kubectl logs`, `kubectl
   rollout restart`, `kubectl describe pvc` — the same commands
   the team already uses for everything else.

If you don't already run EKS and don't plan to, a single EC2 with a
systemd unit running Ollama is genuinely fine. Don't stand up a
cluster just for this.

## Module walkthrough

The module ships under `examples/infra/aws-eks-ollama/`:

```
terraform/      ← namespace, PVC, ServiceAccount (the durable bits)
argocd/         ← ArgoCD Application (the GitOps reconciler)
k8s/            ← Deployment, Service, HPA, init Job (the workload)
README.md       ← step-by-step deployment guide
```

The split is deliberate. **Terraform owns durable primitives** —
namespace, PVC, ServiceAccount — the things you don't want a
GitOps loop accidentally pruning during a sync error. **ArgoCD owns
the workload** — Deployment, Service, HPA, init Job — the things
you want declarative, reconciled, and rollback-able.

If you don't run ArgoCD, the raw `k8s/*.yaml` files apply directly
with `kubectl apply -f ../k8s/`.

### Defaults

- 1 replica
- 4 vCPU / 8 GiB memory limit
- 50 GiB gp3 PVC for `/root/.ollama`
- HPA on CPU at 70 % target, 1–4 replicas
- Init Job pulls `llama3.2:3b` and `nomic-embed-text` on first sync

These are the smallest reasonable production values. They handle
~50 engineer queries/day comfortably. Bump CPU + memory before
moving to GPU; the failure mode "we should have bought more CPU"
is half the cost and a tenth the operational headache of "we
introduced a GPU node group".

## ArgoCD ApplicationSet pattern

A single team can use the shipped `argocd/application.yaml`. A
platform team running multiple vigil brains (one per team) wants
an ApplicationSet:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: vigil-ollama
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - team: platform
            namespace: vigil-platform
          - team: data
            namespace: vigil-data
  template:
    metadata:
      name: 'vigil-ollama-{{ team }}'
    spec:
      project: default
      source:
        repoURL: https://your-team.local/your-org/team-platform.git
        targetRevision: HEAD
        path: infra/ollama/k8s
      destination:
        server: https://kubernetes.default.svc
        namespace: '{{ namespace }}'
      syncPolicy:
        syncOptions:
          - CreateNamespace=true
```

That's the recommended shape once you're past one team. Each team
gets its own namespace + Ollama instance — same blast radius as
before, but generated from a single source.

## DNS / ingress

Three sensible exposure patterns, in increasing order of risk:

| Pattern | Reachability | Use when |
| --- | --- | --- |
| ClusterIP only (default) | In-cluster Pods only | Engineers `kubectl port-forward` from their laptops |
| ClusterIP + private NLB | VPC + VPN | Laptops are already on the corporate VPN |
| Internet-facing LB | Anywhere | **Don't.** Ollama has no auth. |

The default OSS config ships ClusterIP. Engineers run `kubectl
port-forward -n vigil svc/ollama 11434:11434` and point vigil
at `http://localhost:11434`. That's a fine workflow if your team
runs `kubectl` daily anyway.

If port-forwarding is friction, promote to a private NLB through
your service mesh / VPC. Do not promote to a public LB without an
auth proxy in front (which is a Phase C migration, not Phase B).

## Cost notes

For a 5–15 engineer team on a t3.xlarge node:

| Component | Approx monthly | Notes |
| --- | --- | --- |
| Compute (CPU node) | ~$120 | Or fold into existing pool |
| 50 GiB gp3 EBS | ~$4 | Cheap and rarely fills |
| EKS control plane | $0 incremental | Already running |
| **Total incremental** | **~$50–125/mo** | Depends on shared vs dedicated node |

GPU is opt-in later. A g5.xlarge node group runs ~$720/mo on-demand
— ten times the CPU baseline. Move to GPU only after measuring CPU
latency at peak load and confirming it's the bottleneck. Most teams
with ≤50 queries/day on small models stay on CPU forever.

## Validate connectivity from a laptop

Once the cluster is up, verify the Ollama endpoint is reachable
end-to-end:

```bash
# 1. Port-forward in one terminal:
kubectl port-forward -n vigil svc/ollama 11434:11434

# 2. From another terminal, point vigil at it:
cat <<'TOML' >> ~/.vigil/config.toml
[llm]
provider = "ollama"
model    = "llama3.2:3b"
host     = "http://localhost:11434"

[embedding]
provider = "ollama"
model    = "nomic-embed-text"
host     = "http://localhost:11434"
TOML

# 3. Run the diagnostic:
cd ~/team-brain
vigil doctor
```

`vigil doctor` should report:

```
[PASS] config             source=user  llm=ollama:llama3.2:3b  embedding=ollama:nomic-embed-text
[PASS] llm.reachable      http://localhost:11434  120 ms
[PASS] embedding.reachable http://localhost:11434  90 ms
[PASS] models             llama3.2:3b, nomic-embed-text all pulled
```

If `models` reports MISSING, run the init Job again — it pulls them
into the PVC.

## Failure modes

- **Pod OOMKilled on first generation.** The 3B model fits in 6
  GiB RSS comfortably, but a hot path with concurrent requests can
  spike. Bump `memory_limit` to `12Gi` if you see this.
- **PVC stuck Pending.** Storage class `gp3` doesn't exist or has no
  capacity in the AZ. `kubectl describe pvc -n vigil
  ollama-models` will show events.
- **Slow responses.** Check node CPU saturation. Other tenants on
  the same node can throttle Ollama. Either dedicate a node pool
  or move Ollama to a quieter pool.
- **Init Job loops.** `registry.ollama.ai` has had occasional
  outages. The Job has `backoffLimit: 3` — manually re-run if
  needed: `kubectl delete job -n vigil ollama-pull-models &&
  kubectl apply -f k8s/ollama-init-job.yaml`.

## What's deliberately not here

- **No auth.** Ollama itself doesn't authenticate. Phase B's threat
  model assumes the cluster boundary is the trust boundary. Don't
  expose this to the internet.
- **No auto-scaling beyond CPU HPA.** A KEDA-style scaler on
  request queue depth would be nice; it's not in scope for the OSS
  example. PRs welcome.
- **No model pinning.** The init Job pulls `:latest` for the
  configured model names. Pin to digests in a private registry if
  reproducibility matters more than convenience.
- **No GPU profile.** Easy to add (node selector + runtime class +
  resource requests on `nvidia.com/gpu`), but the OSS module stays
  CPU-only so the default install works on every cluster.

Phase B is a starting point — not a finished platform.
