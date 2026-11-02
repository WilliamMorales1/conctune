# How conctune works

Theory behind the report: the four ceilings, the USL fit, the load-balance check, and how the sweep keeps its measurements honest. The [README](../README.md) covers installing and running it.

## What it computes

**Four ceilings on useful concurrency.** The smallest one binds, and which one
binds decides what is worth optimising:

| ceiling | formula | means |
|---|---|---|
| startup | `sqrt(work / B)` | worker boot cost, measured directly |
| overhead | `sqrt((1 - sigma) / kappa)` | USL peak, fitted from the sweep |
| hardware | `cpus * work / cpu_total` | logical CPUs already busy (CPU of the run's process tree) |
| parallelism | `work / span` | longest single case; average parallelism |

**A fitted Universal Scalability Law curve.** `S(N) = N / (1 + sigma(N-1) +
kappa·N(N-1))`, where `sigma` is contention and `kappa` is coherency — the term
that makes scaling go *retrograde*, so that past `N*` more workers means a
slower run. Fitted via Gunther's linearising transform plus a Gauss-Newton
polish, so numpy is the only dependency. The USL is a rational function that
contains Amdahl's law (`kappa = 0`) and Gustafson's scaled speedup as special
cases, and it is the throughput bound of a machine-repairman queue with
state-dependent service [Gunther 2008]. `sigma` is the serial fraction Amdahl
describes; critical sections behave like a serial part plus a parallel part,
which is why lock contention shows up there too [Eyerman & Eeckhout 2010]. A
negative `sigma` is the signature of superlinear speedup (caches that grow with
N). conctune lets `sigma` go negative only when a measured speedup beats N,
and with `kappa > 0` the USL predicts the payback: speedup falls back below N
past `N = -sigma / kappa`, then goes retrograde [Gunther et al. 2015].

The parallelism ceiling is the work-span bound: no scheduler finishes faster
than `max(T_1/N, T_inf)`, and randomised work stealing finishes in expected
`T_1/N + O(T_inf)`, within a constant factor of it [Blumofe & Leiserson 1999].
The usual rule "one worker per core" fails in both directions; measured best
thread counts on a 24-core machine ranged from 16 to 63 [Pusukuri et al.
2011], which is why conctune sweeps instead of assuming.

**A load-balance check.** Measured per-case times are fed to a greedy LPT
schedule; the observed makespan is compared against it (with one worker boot
added back, since the simulation places work but does not boot anything, and
stretched by the run's CPU growth over N=1: idle workers burn no CPU, so CPU
that grows with N means every case ran slower, as when the clock drops under
load, rather than work sitting unassigned). If
the real run trails by more than 25%, scheduling is the problem and the worker
count is not worth tuning yet — a pull queue fixes it and the curve changes
shape afterwards. LPT is never worse than `4/3 - 1/(3N)` times the optimal
makespan [Graham 1969], so it is a sound, slightly pessimistic reference: a
real run trailing it is losing to a simple offline schedule. In open-source test suites a few slow cases rarely
dominate the total [Cândido et al. 2017], but when they do, they set `span` and
the parallelism ceiling binds.

**A reliability gate.** Worker counts where the task failed are excluded from
the recommendation regardless of speed, and reported with their failure rate. A
run that does not finish is not an optimum, and heavily oversubscribed runs are
exactly where timing-sensitive workloads start to flake. Async waits and
concurrency are the leading root causes of flaky tests [Luo et al. 2014;
Parry et al. 2021], and in Python most flaky tests are order-dependent
[Gruber et al. 2021], which parallel runners expose by reshuffling which cases
share a process.

**A recommendation from the measurements, not the model.** The ceilings are
estimates; the sweep is ground truth. conctune recommends the cheapest N within
3% of the fastest measured wall time, then reports the binding ceiling as
diagnosis alongside it — and flags the disagreement when the two differ. When
speedup still climbs at the largest swept N and every ceiling lies beyond it,
no ceiling binds: the report says NOT YET BOUND, names the nearest ceiling, and
asks for a wider sweep.

## User-level and virtual threads

Kernel threads and processes are not the only thing `{j}` can count. Goroutines,
Java virtual threads, Kotlin coroutines, asyncio tasks, and Node promises are
user-level threads: an M:N runtime multiplexes many of them onto a small pool
of kernel threads, its *carriers*. Creating, switching, and parking one avoids
the kernel and costs a small stack, so a runtime can hold tens or hundreds of
thousands of them [Anderson et al. 1992; von Behren et al. 2003b; Karsten &
Barghi 2020], and thread-per-task code written against them matches
event-driven code on throughput [von Behren et al. 2003a]. That moves the
question. There are two knobs, and conctune
measures one at a time:

| knob | examples | what bounds it |
|---|---|---|
| carriers | `GOMAXPROCS`, `jdk.virtualThreadScheduler.parallelism`, asyncio and Node: always 1 | cores, exactly like kernel workers; leave at the core count unless the sweep says otherwise |
| tasks in flight | semaphore size, connection-pool size, `errgroup.SetLimit`, `asyncio.Semaphore(n)` | Little's law and the slowest downstream resource, not the cores |

**Little's law sets the target.** In steady state the number in flight equals
throughput times time in system, `L = lambda * W` [Little 1961]. If each task
computes for `C` and waits for `W`, a carrier stays busy with
`1 + W/C` tasks in flight, so the useful concurrency is
`carriers * (1 + W/C)` [Goetz et al. 2006, §8.2]. That is the hardware ceiling
conctune already computes, `cpus * work / cpu_total`, since `work / cpu_total`
is `(W + C) / C`. The only change for user-level threads is the multiplier:
set `[run].carriers` and it counts carriers, not CPUs. Without it an asyncio
service on a laptop with 8 logical CPUs would be told it can use eight times
the concurrency its single loop can actually feed.

`cpus` is the logical CPU count, not the physical core count. CPU time is
billed per logical CPU, so two SMT siblings sharing a core each bill their full
wall time: if SMT adds nothing, `cpu_total` doubles at saturation and the
ceiling lands back at the physical cores; if it adds 25% throughput, the
ceiling lands at 1.25x the physical cores. Counting physical cores would price
SMT at nothing, and an SMT machine would beat its own hardware ceiling on
every CPU-bound sweep.

`cpu_total` is read at the worker count nearest the CPU (or carrier) count,
then, for blocking work, at the count nearest the ceiling it implies, until the
two agree. A run far below saturation burns more CPU per case (idle wake-ups, a
clock that never ramps up), so reading it there would understate how many
blocking tasks fit. When per-case times sum to more than 1.25x the whole N=1
run, they carry costs the real run never pays, so the N=1 wall time stands in
for `work` and the report flags `work_overstated`.

**The downstream binds before the threads do.** Because a virtual thread costs
almost nothing, the old reason to cap concurrency, thread cost, is gone. JEP
444 tells Java users not to pool virtual threads at all and to put a semaphore
on the scarce resource instead. The optimum is then set by that resource (a
database pool, a rate limit, a remote service's own USL curve), and it shows up
in the fit as `sigma` (a pool that queues) or `kappa` (a downstream that
thrashes), not in the startup ceiling, which is effectively infinite.

**Cooperative scheduling changes the failure mode.** asyncio and Node never
preempt a task; Java virtual threads are not time-sliced either, and before
JDK 24 (JEP 491) a virtual thread inside `synchronized` pinned its carrier. Go
has preempted goroutines asynchronously since 1.14. On a cooperative carrier
one CPU-heavy task stalls every other task on it, so tasks that finish their
waits together queue for the carrier in a burst (a convoy) well before it is
saturated. That is contention, but a schedule simulation cannot tell it from
imbalance, so with `carriers` set conctune skips the load-balance check.

**Sweep differently.** The machine's suggested points stop at twice the
logical core count; a task that waits 95% of the time needs points out to
tens of times that. Sweep geometrically (`1,4,16,64,256,1024`), and keep
points past the plateau so `kappa` stays identifiable.

Measured: `examples/asyncio.toml`, 160 tasks that each wait 200 ms and compute
8 ms on one loop (`W/C = 25`), downstream pool of 48, on an i7-8650U
(4 physical cores):

| N in flight | wall (s) | speedup |
|---:|---:|---:|
| 1 | 33.49 | 1.00 |
| 4 | 9.44 | 3.55 |
| 16 | 3.40 | 9.87 |
| 32 | 2.39 | 14.01 |
| 48 | 2.19 | 15.28 |
| 64 | 2.18 | 15.34 |
| 256 | 2.19 | 15.33 |

Hardware ceiling with `carriers = 1`: about 23 (counting the 8 logical CPUs
would have said about 180).
Recommended: 48. The curve flattens at about twice the ceiling rather than at
it because cases arrive in bursts, so a little extra in flight keeps the loop
fed through the gaps; past 48 nothing changes, because the loop is the
bottleneck and the pool cap is never reached.

For services rather than batch jobs, the same trade-off is made continuously:
SEDA resizes each stage's thread pool from its queue length [Welsh et al.
2001], and Shenango and Caladan reallocate cores between runtimes every few
microseconds from queueing delay [Ousterhout et al. 2019; Fried et al. 2020].
conctune answers the offline version: one fixed N for a known workload.

## Measurement hygiene

Small, innocent-looking setup details (environment size, link order) bias
timings enough to reverse conclusions [Mytkowicz et al. 2009], and performance
results in the literature routinely omit the variability needed to tell signal
from noise [Hoefler & Belli 2015; Uta et al. 2020]. Randomising the setup is
the standard defence [Mytkowicz et al. 2009; Curtsinger & Berger 2013], and
interleaving the conditions on one machine in random order is what lets small
differences be detected on noisy hosts [Laaber et al. 2019].

Handled for you:

- run order is randomised across the whole (points x repeats) matrix, so
  thermal drift cannot correlate with the worker count and manufacture fake
  retrograde scaling;
- repeats collapse by median, not mean, because a throttling laptop has a long
  right tail;
- a warmup run is discarded (one run is a floor, not a guarantee: JIT runtimes
  often never reach a steady state [Barrett et al. 2017]);
- each run starts in its own child cgroup, whose `cpu.stat` counts the whole
  process tree (forkserver workers and daemons the harness never parented) and
  nothing else on the machine. Without a delegated cgroup v2 (a systemd user
  session gives one) it falls back to `/proc/stat` deltas, which count every
  process, and the report says so. macOS uses `getrusage(RUSAGE_CHILDREN)`,
  which counts the run's tree but misses processes it detaches; Windows uses
  `GetSystemTimes` deltas, which, like `/proc/stat`, count every process;
- sweep points are chosen dense below the physical core count and sparse out to
  2x logical, because without points past saturation `kappa` is unidentifiable.

Flagged, not fixed: a busy machine, a ramping frequency governor, turbo boost,
SMT, and low free memory all distort the curve. The report names each one it
finds.

## Reading the result

- **R² below 0.90** — the USL does not describe this workload. Usually load
  imbalance that varies with N, or thermal drift. Fix that before trusting `N*`.
- **kappa ≈ 0** — no retrograde term; scaling is Amdahl-shaped and the ceiling
  is `1/sigma`. Sweep further past the core count if you need to pin it down.
- **sigma < 0** — superlinear speedup, usually per-worker caches or memory
  that grow with N. The report gives the payback point `-sigma / kappa`.
- **fastest point is the largest swept** — the peak may be beyond your range;
  extend `--points`.
- **hardware ceiling below the measured optimum** — the ceilings are
  first-order estimates; workers past them still win, just sub-linearly.
- **hardware ceiling *above* the logical CPU count** — the cases block rather than
  compute (waiting on I/O, a language server, a subprocess), so oversubscribing
  pays. The report says by how much.
- **rows marked `(saturated)` in the balance table** — past the physical core
  count (further for a workload that blocks) a case no longer runs at its
  contention-free service time, so the simulated schedule is optimistic there
  and those rows are excluded from the judgement.
- **`work_overstated`** — the per-case times are too high: either boot was
  never measured, or they sum to more than 1.25x the whole N=1 run. The
  parallelism ceiling and balance table inherit the error; prefer log harvest.

## References

USL and scaling laws

- Gunther, N. J. *Guerrilla Capacity Planning*. Springer, 2007.
- Gunther, N. J. "A General Theory of Computational Scalability Based on
  Rational Functions." arXiv:0808.1431, 2008.
- Gunther, N. J., Puglia, P., Tomasette, K. "Hadoop Superlinear Scalability."
  *ACM Queue* 13(5), 2015. doi:10.1145/2773212.2789974
- Hill, M. D., Marty, M. R. "Amdahl's Law in the Multicore Era." *IEEE
  Computer* 41(7):33-38, 2008. doi:10.1109/MC.2008.209
- Eyerman, S., Eeckhout, L. "Modeling Critical Sections in Amdahl's Law and its
  Implications for Multicore Design." ISCA 2010.
  doi:10.1145/1816038.1816011

Scheduling and choosing N

- Graham, R. L. "Bounds on Multiprocessing Timing Anomalies." *SIAM J. Appl.
  Math.* 17(2):416-429, 1969. doi:10.1137/0117039
- Blumofe, R. D., Leiserson, C. E. "Scheduling Multithreaded Computations by
  Work Stealing." *J. ACM* 46(5):720-748, 1999. doi:10.1145/324133.324234
- Pusukuri, K. K., Gupta, R., Bhuyan, L. N. "Thread Reinforcer: Dynamically
  Determining Number of Threads via OS Level Monitoring." IISWC 2011.
- Harchol-Balter, M. *Performance Modeling and Design of Computer Systems*.
  Cambridge University Press, 2013.
- Dean, J., Barroso, L. A. "The Tail at Scale." *CACM* 56(2):74-80, 2013.
- Baumann, A., Appavoo, J., Krieger, O., Roscoe, T. "A fork() in the road."
  HotOS 2019. doi:10.1145/3317550.3321435 (why worker boot is not free).
- Curtsinger, C., Berger, E. D. "Coz: Finding Code that Counts with Causal
  Profiling." SOSP 2015 (once a ceiling binds, which code to speed up).

User-level threads

- Little, J. D. C. "A Proof for the Queuing Formula: L = λW." *Operations
  Research* 9(3):383-387, 1961.
- Anderson, T. E., Bershad, B. N., Lazowska, E. D., Levy, H. M. "Scheduler
  Activations: Effective Kernel Support for the User-Level Management of
  Parallelism." *ACM TOCS* 10(1):53-79, 1992.
- Welsh, M., Culler, D., Brewer, E. "SEDA: An Architecture for
  Well-Conditioned, Scalable Internet Services." SOSP 2001.
  doi:10.1145/502034.502057
- von Behren, R., Condit, J., Brewer, E. "Why Events Are a Bad Idea (for
  High-Concurrency Servers)." HotOS 2003. (2003a)
- von Behren, R., Condit, J., Zhou, F., Necula, G. C., Brewer, E. "Capriccio:
  Scalable Threads for Internet Services." SOSP 2003.
  doi:10.1145/945445.945471 (2003b)
- Goetz, B., et al. *Java Concurrency in Practice*. Addison-Wesley, 2006.
  §8.2, sizing thread pools.
- Ousterhout, A., Fried, J., Behrens, J., Belay, A., Balakrishnan, H.
  "Shenango: Achieving High CPU Efficiency for Latency-Sensitive Datacenter
  Workloads." NSDI 2019.
- Fried, J., Ruan, Z., Ousterhout, A., Belay, A. "Caladan: Mitigating
  Interference at Microsecond Timescales." OSDI 2020.
- Karsten, M., Barghi, S. "User-level Threading: Have Your Cake and Eat It
  Too." *Proc. ACM Meas. Anal. Comput. Syst.* 4(1), 2020.
- Elizarov, R., Belyaev, M., Akhin, M., Usmanov, I. "Kotlin Coroutines: Design
  and Implementation." Onward! 2021.
- OpenJDK JEP 444, "Virtual Threads" (JDK 21, 2023), and JEP 491, "Synchronize
  Virtual Threads without Pinning" (JDK 24, 2025).

Measurement methodology

- Mytkowicz, T., Diwan, A., Hauswirth, M., Sweeney, P. F. "Producing Wrong
  Data Without Doing Anything Obviously Wrong!" ASPLOS 2009.
  doi:10.1145/1508284.1508275
- Curtsinger, C., Berger, E. D. "STABILIZER: Statistically Sound Performance
  Evaluation." ASPLOS 2013.
- Hoefler, T., Belli, R. "Scientific Benchmarking of Parallel Computing
  Systems: Twelve Ways to Tell the Masses when Reporting Performance Results."
  SC 2015. doi:10.1145/2807591.2807644
- Barrett, E., Bolz-Tereick, C. F., Killick, R., Mount, S., Tratt, L. "Virtual
  Machine Warmup Blows Hot and Cold." OOPSLA 2017. doi:10.1145/3133876
- Laaber, C., Scheuner, J., Leitner, P. "Software Microbenchmarking in the
  Cloud. How Bad is it Really?" *Empirical Software Engineering* 24(4), 2019.
  doi:10.1007/s10664-019-09681-1
- Uta, A., et al. "Is Big Data Performance Reproducible in Modern Cloud
  Networks?" NSDI 2020.

Parallel test suites and flakiness

- Luo, Q., Hariri, F., Eloussi, L., Marinov, D. "An Empirical Analysis of Flaky
  Tests." FSE 2014.
- Cândido, J., Melo, L., d'Amorim, M. "Test Suite Parallelization in
  Open-Source Projects: A Study on Its Usage and Impact." ASE 2017.
- Gruber, M., Lukasczyk, S., Kroiß, F., Fraser, G. "An Empirical Study of Flaky
  Tests in Python." ICST 2021. arXiv:2101.09077
- Parry, O., Kapfhammer, G. M., Hilton, M., McMinn, P. "A Survey of Flaky
  Tests." *ACM TOSEM* 31(1), 2021.
- Mondal, S., Silva, D., d'Amorim, M. "Soundy Automated Parallelization of Test
  Execution." ICSME 2021.
