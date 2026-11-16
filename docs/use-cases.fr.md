# Idées et recettes

*[English version](use-cases.md)*

Tout ce que Blade permet de faire dans Home Assistant, fonction par fonction. Chaque idée précise le service Blade qu'elle utilise, et les recettes sont des points de départ à adapter. Les identifiants comme `sensor.puissance_congelateur` sont des exemples : mettez les vôtres à la place.

Les recettes utilisent les identifiants créés par défaut : `ai_task.blade_ai_task`, `ai_task.blade_evaluation`, `conversation.blade_conversation`, `stt.blade_stt` et `tts.blade_tts`.

## Sommaire

- [Décider avec des probabilités calibrées](#décider-avec-des-probabilités-calibrées)
- [Lire et comprendre](#lire-et-comprendre)
- [Anticiper](#anticiper)
- [Écrire et créer](#écrire-et-créer)
- [Parler à sa maison](#parler-à-sa-maison)
- [Surveiller la dépense](#surveiller-la-dépense)
- [Bonnes pratiques](#bonnes-pratiques)

## Décider avec des probabilités calibrées

`ai_task.evaluate` répond par une probabilité à des questions par oui ou non, à choix ou à note. L'appel est rapide et ne produit aucun texte : c'est la façon la plus économique de donner du discernement à une automatisation.

**Idées**

- **Moins de notifications, mais des utiles.** Avant une alerte de porte, de mouvement ou de boîte aux lettres, demandez s'il faut vraiment prévenir, et n'envoyez la notification qu'au-delà d'un seuil.
- **Savoir si quelqu'un est là, sans capteur de présence.** Les téléphones connectés au Wi-Fi, un détecteur de couloir, les lumières, la télé, la consommation du PC ou l'humidité de la salle de bain suffisent pour estimer la probabilité que quelqu'un soit là, et qu'il soit réveillé.
- **Choisir le bon mode.** Laissez Blade régler le purificateur, le chauffage ou les volets selon la qualité de l'air, la météo, l'heure et la présence.
- **Mesurer l'urgence.** Une fuite, une pièce trop froide, une fenêtre restée ouverte : notez l'urgence de 0 à 3 et réagissez en fonction.
- **Faire le tri.** Rangez l'objet d'un courriel ou une tâche dans une catégorie grâce à une question à choix.

**Recette : ne prévenir que quand ça compte**

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.boite_aux_lettres
    to: "on"
actions:
  - action: ai_task.evaluate
    data:
      task_name: Boîte aux lettres
      entity_id: ai_task.blade_evaluation
      state: >-
        Détecteur de la boîte aux lettres déclenché à {{ now().strftime('%H:%M') }}.
        Courrier déjà relevé aujourd'hui : {{ states('input_boolean.courrier_releve') }}.
      questions:
        alerte:
          type: noul
          instructions: Est-ce une activité suspecte qui mérite une alerte immédiate, plutôt qu'une distribution normale ?
    response_variable: avis
  - condition: template
    value_template: "{{ avis.answers.alerte.noul >= 0.6 }}"
  - action: notify.mobile_app_mon_telephone
    data:
      message: "Activité suspecte à la boîte aux lettres ({{ (avis.answers.alerte.noul * 100) | round }} %)."
```

**Recette : estimer la présence**

Créez une entrée `input_number.presence_estimee` (de 0 à 100), puis lancez ceci toutes les cinq minutes :

```yaml
actions:
  - action: ai_task.evaluate
    data:
      task_name: Présence
      entity_id: ai_task.blade_evaluation
      state: >-
        Heure : {{ now().strftime('%H:%M') }}.
        Téléphone sur le Wi-Fi : {{ states('device_tracker.mon_telephone') }}.
        Mouvement dans le couloir : {{ states('binary_sensor.couloir') }}, dernier changement {{ relative_time(states.binary_sensor.couloir.last_changed) }}.
        Lumières allumées : {{ states.light | selectattr('state', 'eq', 'on') | map(attribute='name') | join(', ') or 'aucune' }}.
        Consommation du PC : {{ states('sensor.puissance_pc') }} W.
      questions:
        present:
          type: noul
          instructions: Y a-t-il quelqu'un dans le logement en ce moment ?
    response_variable: avis
  - action: input_number.set_value
    target:
      entity_id: input_number.presence_estimee
    data:
      value: "{{ (avis.answers.present.noul * 100) | round }}"
```

## Lire et comprendre

**Idées**

- **Le courrier papier devient une tâche.** Prenez une lettre en photo : `ai_task.generate_data`, avec la photo en pièce jointe, en extrait l'expéditeur, le montant et l'échéance, et vous en faites une tâche datée.
- **Relever un compteur ou un écran.** `blade.ocr` sur une capture de caméra lit un compteur d'eau, l'écran d'une chaudière ou l'étiquette d'un colis.
- **Décrire ce que voit une caméra.** Joignez une capture et demandez qui ou quoi se trouve devant la porte ; la description part avec la notification.
- **Lire une page web.** Avec *Lecture de pages web* activée, l'agent de conversation ouvre la page que vous lui indiquez et vous la résume.

**Recette : une lettre devient une tâche**

```yaml
fields:
  lettre:
    selector:
      media:
        accept: [image/*]
sequence:
  - action: ai_task.generate_data
    data:
      task_name: Courrier
      entity_id: ai_task.blade_ai_task
      instructions: Lis ce courrier. Donne l'expéditeur, ce qu'il faut faire en une phrase courte, et la date limite au format AAAA-MM-JJ s'il y en a une.
      structure:
        expediteur: {selector: {text: {}}, required: true}
        action: {selector: {text: {}}, required: true}
        echeance: {selector: {text: {}}}
      attachments:
        - "{{ lettre }}"
    response_variable: courrier
  - action: todo.add_item
    target:
      entity_id: todo.taches
    data:
      item: "{{ courrier.data.expediteur }} : {{ courrier.data.action }}"
```

**Recette : relever un compteur**

```yaml
- action: blade.ocr
  data:
    camera_entity_id: camera.compteur_eau
    instructions: Donne uniquement le nombre affiché par le compteur.
  response_variable: releve
```

## Anticiper

`blade.forecast` prévoit les prochaines heures de tout capteur doté de statistiques long terme, avec une fourchette probable.

**Idées**

- **L'heure la plus verte.** Prévoyez l'intensité carbone du réseau électrique et lancez le lave-linge au meilleur moment.
- **Garder un œil sur un appareil.** Comparez la consommation d'un congélateur ou d'une pompe à chaleur à sa prévision : si elle sort de la fourchette, quelque chose ne va pas.
- **Chauffer avant d'avoir froid.** Prévoyez la température intérieure et lancez le chauffage avant qu'elle descende sous le seuil de confort.
- **Solaire et batterie.** Prévoyez la production ou la consommation pour savoir quand charger et quand puiser dans la batterie.

**Recette : l'heure la plus verte**

```yaml
- action: blade.forecast
  data:
    entity_id: sensor.intensite_carbone
    history_hours: 336
    horizon_hours: 16
  response_variable: prevision
- variables:
    meilleure: "{{ prevision.forecast | sort(attribute='value') | first }}"
- action: notify.mobile_app_mon_telephone
  data:
    message: "Moment le plus vert aujourd'hui : {{ as_local(as_datetime(meilleure.datetime)).strftime('%H:%M') }}."
```

## Écrire et créer

**Idées**

- **Le bilan du soir.** L'énergie consommée dans la journée, le programme du lendemain et la météo, en quatre lignes sur votre téléphone.
- **Des cartes d'anniversaire.** Le jour d'un anniversaire inscrit au calendrier, un petit mot personnel accompagné d'une carte dessinée par `ai_task.generate_image`.
- **Traduire.** `blade.translate` pour écrire à un invité dans sa langue ou comprendre la notice d'un appareil.
- **Les courses rangées par rayon.** Avant de partir, la liste de courses réorganisée dans l'ordre des rayons.
- **Remettre de l'ordre dans un texte libre.** Un courriel de livraison ou une note deviennent des champs exploitables grâce à une `structure`.

**Recette : les courses par rayon**

```yaml
- action: todo.get_items
  target:
    entity_id: todo.liste_de_courses
  data:
    status: needs_action
  response_variable: articles
- action: ai_task.generate_data
  data:
    task_name: Courses par rayon
    entity_id: ai_task.blade_ai_task
    instructions: >-
      Range ces articles par rayon de supermarché, une ligne par rayon :
      {{ articles['todo.liste_de_courses']['items'] | map(attribute='summary') | join(', ') }}
  response_variable: rangement
- action: notify.mobile_app_mon_telephone
  data:
    message: "{{ rangement.data }}"
```

## Parler à sa maison

**Idées**

- **Un assistant vocal prêt à servir.** L'assistant Blade créé à l'installation réunit l'agent, la reconnaissance vocale et la synthèse vocale en streaming.
- **Un personnage à vous.** Décrivez-lui un caractère dans les instructions ; grâce aux templates, il connaît l'heure, la météo ou le prochain train.
- **Vos propres outils.** Collez vos fonctions d'Extended OpenAI Conversation (`native` execute_service, `script`, `template`) : l'agent pourra lancer vos recherches, tenir vos listes ou consulter vos prévisions.
- **Des annonces à voix haute.** Un second agent, sans outils, rédige de courtes annonces (lessive terminée, porte restée ouverte) qu'une synthèse vocale diffuse dans la maison.
- **Des calculs exacts.** Avec *Exécuter du code Python*, l'agent calcule au lieu de deviner : dates, conversions, moyennes.

**Recette : une fonction que l'agent peut appeler**

Dans le champ **Fonctions personnalisées** de l'agent :

```yaml
- spec:
    name: get_entity_state
    description: Lit l'état et les attributs d'une entité, sans rien modifier.
    parameters:
      type: object
      properties:
        entity_id:
          type: string
      required: [entity_id]
  function:
    type: template
    value_template: >-
      {{ states(entity_id) }} {{ states[entity_id].attributes | tojson if states[entity_id] else '' }}
```

## Surveiller la dépense

**Idées**

- **Un plafond mensuel.** Réglez-le dans les options de l'intégration : une fois l'estimation arrivée au plafond, plus aucun appel ne part vers Blade.
- **Être prévenu à temps.** Recevez une notification à 80 % du plafond grâce au capteur *Coût estimé ce mois-ci*.
- **Savoir ce qui coûte.** Les capteurs de coût détaillent la dépense par service : vous voyez tout de suite quel agent ou quelle automatisation consomme le plus.
- **Réagir à un refus.** Déclenchez une automatisation quand *État du compte* n'est plus à `ok`, pour savoir aussitôt que le solde est épuisé.

**Recette : une alerte à 80 % d'un plafond de 5 €**

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.blade_cout_estime_ce_mois_ci
    above: 4
actions:
  - action: notify.mobile_app_mon_telephone
    data:
      message: "Blade a coûté {{ states('sensor.blade_cout_estime_ce_mois_ci') }} € ce mois-ci."
```

## Bonnes pratiques

- Mettez les faits dans `state` et la question dans `instructions` : plus les faits sont courts et précis, plus les probabilités sont justes.
- N'agissez qu'au-delà d'un seuil (entre 0,6 et 0,8), et gardez la probabilité dans le message pour pouvoir ajuster ce seuil.
- Si vous n'avez besoin que d'une décision, préférez `ai_task.evaluate` à la génération de texte : c'est plus rapide et moins cher.
- Pour les automatisations fréquentes, envoyez peu de texte ; réservez les longs prompts aux cas qui le demandent.
- Laissez la recherche web désactivée si vous n'en avez pas besoin : elle est facturée à chaque recherche.
