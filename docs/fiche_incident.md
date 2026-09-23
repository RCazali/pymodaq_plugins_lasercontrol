# Fiche incident — Laser Control (T11.2)

> **Réflexe** : si le laser ou l'optique est en danger (faisceau qui part, échauffement, odeur, bruit
> anormal), **réduire la puissance de pompe à la main**. L'extension ne commande pas la pompe. C'est la
> seule mise en sécurité physique ultime (D6).
>
> Ensuite seulement : **ARRÊT D'URGENCE** (bouton rouge, **F12** ou **Pause**), observer, lire le journal.

Ce que le logiciel garantit : en automatique, les moteurs ne font que des **pas relatifs bornés**
(S3), dans une **enveloppe** autour de la position d'engagement (S4), avec un **débit** limité (S5).
Aucun ordre n'est mis en file (S10). Un plantage laisse donc les moteurs immobiles.

---

## 1. Arrêt d'urgence déclenché (par vous ou par quelqu'un)

1. Le badge indique **ARRÊT D'URGENCE**. Tous les automatismes sont arrêtés et un `stop_motion` a été
   envoyé à **tous** les moteurs du dashboard (S11). L'état reste verrouillé.
2. Regarder ce qui bougeait : les cartes de la Vue d'ensemble et le journal (les dernières lignes
   avant « ARRÊT D'URGENCE »).
3. Si un moteur semble encore bouger (contrôleur lent à s'arrêter, jog `limit`), utiliser le bouton
   stop du module PyMoDAQ ou le contrôleur lui-même.
4. Une fois la situation comprise : **Réarmer** (barre d'état). Rien ne redémarre : relancer à la main
   ce qui doit l'être.

## 2. L'extension ou PyMoDAQ plante, se fige ou se ferme

1. **Pompe** si nécessaire (voir le réflexe).
2. Moteurs : aucun automatisme ne tourne plus et aucun ordre n'est en file. Les derniers pas envoyés
   sont courts. **Exception** : un jog en mode `limit` (Alignement) peut continuer jusqu'à Min/Max sur
   le contrôleur. L'arrêter sur le contrôleur (ou le mettre hors tension) si c'est le cas.
3. Fermer PyMoDAQ, en tuant le processus s'il ne répond plus.
4. Relancer dans l'ordre : dashboard, puis extension. **Rien ne démarre seul** (S9). L'interface est
   restaurée, sans automatisme actif.
5. Avant de réactiver : vérifier les positions (Vue d'ensemble, **⟳ Lire** dans Alignement) et les
   centroïdes caméra.
6. Pour comprendre : le journal technique `~/.pymodaq/laser_control/logs/laser_control.log` et le
   fichier d'enregistrement de la session (lisible jusqu'à la dernière écriture).

## 3. Ordi B muet (voyant rouge, « données périmées », « compteur de trames figé »)

Automatiquement : les automatismes qui utilisent ses données sont **gelés** (HELD, S1). Aucun moteur
ne bouge et l'intégrale des PID est gelée.

1. Rien à faire côté moteurs.
2. Côté ordi B : l'extension caméras est-elle en acquisition ? Le coordinateur LECO tourne-t-il ? Le
   réseau fonctionne-t-il ?
3. Côté ordi A : le voyant doit redevenir vert. S'il reste gris ou rouge, cliquer **▶ Acquisition**, ou
   réinitialiser le DAQ_Viewer LECO dans le dashboard.
4. Vérifier les centroïdes, puis **Reprendre** (Vue d'ensemble, ou un clic sur chaque bouton rouge de
   l'onglet Alignement), une fois les valeurs revenues (O10-A).

## 4. Intensité à 0 ou chute (pompe coupée, mode-lock perdu)

Automatiquement : **gel** dès que l'intensité passe sous le seuil (S2).

1. Cliquer **ML OFF** (ou **Instable**) pour l'annoter, et ajouter une **Note** si une cause est connue.
2. Ne pas relancer les PID pour « chercher » le laser : ils ne peuvent pas retrouver un laser éteint.
3. Rétablir le laser à la main (pompe, mode-lock). Au besoin, désactiver la stabilisation et réaligner
   depuis l'onglet Alignement.
4. **Reprendre** n'est accepté qu'au-dessus du seuil plus l'hystérésis. La référence reste celle prise
   à l'engagement. Si la puissance de travail a changé volontairement : **Désactiver** puis
   **Activer**, et la référence est reprise à ce moment-là.
5. **ML ON** quand le mode-lock est revenu.

## 5. Un moteur ne répond plus (« délai dépassé »)

1. L'automatisme concerné est gelé et n'envoie plus rien à ce moteur.
2. Vérifier le contrôleur : alimentation, câble, état du module dans le dashboard. Au besoin,
   réinitialiser le module.
3. Relire la position (**⟳ Lire**) avant de relancer quoi que ce soit.

## 6. Enveloppe ou débit atteint (« enveloppe atteinte », « limite de débit »)

Le PID ou l'optimiseur a poussé un moteur jusqu'à sa limite. Il est gelé et le moteur est resté dans
l'enveloppe.

1. **Ne pas reprendre en aveugle.** Regarder la courbe du PID : diverge-t-elle ? Le signe est-il faux ?
   Le gain trop fort ? La dérive réelle dépasse-t-elle l'excursion autorisée ?
2. Si la position est correcte : **Nouvelle référence d'enveloppe** (onglet PID), puis **Reprendre**.
3. Sinon : **Désactiver**, recentrer à la main, corriger le réglage (`reglage_pid.md`), puis réactiver.

## 7. Automatisme en « Erreur »

Une exception dans son calcul l'a arrêté (S8), sans nouvelle tentative. Lire le message dans le
journal et relancer une fois. Si l'erreur revient, noter le message et le preset utilisé.

## Après un incident

- Ajouter une **Note** qui décrit ce qui s'est passé.
- Conserver le fichier HDF5 de la session et le journal technique.
- Si c'est reproductible, rejouer le scénario au bureau avec le simulateur. Le viewer `SimB` permet de
  rendre l'ordi B muet, de figer ses valeurs ou de provoquer des chutes d'intensité.
