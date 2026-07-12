---
title: Lecture 02
course: class 1
date: '2026-07-03'
lecture_number: 2
tags:
- lecture-notes
- class-1
source_pdf: /Users/danielharrington/Desktop/notex project/notes_raw/class_1/lecture_02.pdf
processed: '2026-07-11'
---
If $[H, \Pi] = 0$, and $\ket{n}$ is non-degenerate eigenstate of $H$, then $\ket{n}$ is parity eigenstate.

ex)
SHO
$a^{\dagger} \sim \hat{x} - i\hat{p}$ so parity odd, $\Pi a^{\dagger} \Pi = -a^{\dagger}$
Then

$$
\Pi a^{\dagger}\ket{0} = -\ket{1}
$$

In general,

$$
\Pi\ket{n} = (-1)^n\ket{n}
$$

Hydrogen
g.s. in $|x\rangle$: $\ket{1,0,0}$
$\langle\vec{x}|1,0,0\rangle = R_{1,0}(r) Y_0^0(\theta, \phi) = \left(\frac{z}{a_0}\right)^{3/2} e^{-z/a_0} \frac{1}{\sqrt{4\pi}}$, parity even
$1^{\text{st}}$ excited: $|n,l,m_l\rangle = \ket{2,0,0}$ (degenerate with $l=1, m_l$)
Superposition: $\ket{\psi} = C_p\ket{n=2, l=1} + C_s\ket{n=2, l=0}$, not parity eigenstate.

Selection rules:
Two states $\ket{\alpha}, \ket{\beta}$ with $\Pi\ket{\alpha} = \varepsilon_\alpha\ket{\alpha}, \Pi\ket{\beta} = \varepsilon_\beta\ket{\beta}$
Dipole transition, $\alpha$ to $\beta$:

$$
\begin{aligned}
\langle\beta|x|\alpha\rangle &= \langle\beta|\Pi(\Pi^{\dagger} x \Pi)\Pi^{\dagger}|\alpha\rangle \\
&= \langle\beta|\varepsilon_\beta(-x)\varepsilon_\alpha|\alpha\rangle \\
&= -\varepsilon_\alpha\varepsilon_\beta\langle\beta|x|\alpha\rangle
\end{aligned}
$$

$\Rightarrow \varepsilon_\alpha\varepsilon_\beta = -1$, must have opposite parity

Mann's rule:
Transition $\langle\psi_f|v|\psi_i\rangle$, $i$ $(f)$ has parity $\varepsilon_i$ $(\varepsilon_f)$, $v$ has parity $\varepsilon_v$
Requires: $\varepsilon_f = \varepsilon_v\varepsilon_i \Leftrightarrow \varepsilon_f\varepsilon_v\varepsilon_i = +1$.

ex)
Double well
Left-right states:

![Figure 1 @darkmode](figures/lecture_02_fig_001.jpg)

$$
\begin{aligned}
|L\rangle &= \frac{1}{\sqrt{2}}(|S\rangle + |A\rangle) \\
|R\rangle &= \frac{1}{\sqrt{2}}(|S\rangle - |A\rangle)
\end{aligned}
$$

Non-stationary, oscillate between $L, R$ with frequency

$$
\omega = \frac{\varepsilon_a - \varepsilon_s}{\hbar}
$$