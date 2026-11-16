# Ideas and recipes

*[Version française](use-cases.fr.md)*

What you can build with Blade in Home Assistant, feature by feature. Each idea names the Blade service it relies on; the recipes are starting points to adapt. Entity ids such as `sensor.freezer_power` are examples: replace them with yours.

Default entity ids used below: `ai_task.blade_ai_task`, `ai_task.blade_evaluation`, `conversation.blade_conversation`, `stt.blade_stt`, `tts.blade_tts`.

## Contents

- [Decide with calibrated probabilities](#decide-with-calibrated-probabilities)
- [Read and understand](#read-and-understand)
- [Anticipate](#anticipate)
- [Write and create](#write-and-create)
- [Talk to your home](#talk-to-your-home)
- [Keep spend in check](#keep-spend-in-check)
- [Good practice](#good-practice)

## Decide with calibrated probabilities

`ai_task.evaluate` answers yes/no, choice and score questions with a probability, in one fast call that generates no text. It is the cheapest way to put judgement into an automation.

**Ideas**

- **Notifications that are worth it.** Ask "is this worth waking someone for?" before sending a door, motion or mailbox alert, and only notify above a threshold.
- **Presence without presence sensors.** Combine phones on Wi-Fi, a hallway sensor, lights, TV, PC power and bathroom humidity into a single "someone is home" and "someone is awake" probability.
- **Pick a mode.** Let Blade choose the air purifier, heating or blind mode from air quality, weather, time and occupancy.
- **Rate a situation.** Score how urgent a leak, a cold room or an open window is, from 0 to 3, and react accordingly.
- **Sort messages.** Classify an incoming email subject or a todo item into categories with a `choice` question.

**Recipe: notify only when it matters**

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.mailbox_motion
    to: "on"
actions:
  - action: ai_task.evaluate
    data:
      task_name: Mailbox
      entity_id: ai_task.blade_evaluation
      state: >-
        Mailbox sensor triggered on {{ now().strftime('%A at %H:%M') }}.
        Mail already collected today: {{ states('input_boolean.mail_collected') }}.
      questions:
        alert:
          type: noul
          instructions: Is this suspicious activity worth an immediate alert, rather than a normal delivery?
    response_variable: verdict
  - condition: template
    value_template: "{{ verdict.answers.alert.noul >= 0.6 }}"
  - action: notify.mobile_app_my_phone
    data:
      message: "Suspicious activity at the mailbox ({{ (verdict.answers.alert.noul * 100) | round }} %)."
```

**Recipe: estimate presence into a helper**

Create an `input_number.presence_estimate` helper (0 to 100), then run this every five minutes:

```yaml
actions:
  - action: ai_task.evaluate
    data:
      task_name: Presence
      entity_id: ai_task.blade_evaluation
      state: >-
        Time: {{ now().strftime('%A %H:%M') }}.
        Phone on Wi-Fi: {{ states('device_tracker.my_phone') }}.
        Hallway motion: {{ states('binary_sensor.hallway_motion') }}, last change {{ relative_time(states.binary_sensor.hallway_motion.last_changed) }} ago.
        Lights on: {{ states.light | selectattr('state', 'eq', 'on') | map(attribute='name') | join(', ') or 'none' }}.
        PC power: {{ states('sensor.pc_power') }} W.
      questions:
        present:
          type: noul
          instructions: Is someone at home right now?
    response_variable: verdict
  - action: input_number.set_value
    target:
      entity_id: input_number.presence_estimate
    data:
      value: "{{ (verdict.answers.present.noul * 100) | round }}"
```

## Read and understand

**Ideas**

- **Paper mail into tasks.** Photograph a letter; `ai_task.generate_data` with the photo attached extracts sender, amount and due date into a todo item with a due date.
- **Read a screen or a meter.** `blade.ocr` on a camera snapshot of a water meter, a boiler display or a parcel label.
- **Describe a camera scene.** "Who or what is at the door?" with a camera snapshot attached; send the description with the notification.
- **Check a page.** With *Read web pages* on, the conversation agent can open a page you name and summarise it.

**Recipe: a letter becomes a task**

```yaml
fields:
  letter:
    selector:
      media:
        accept: [image/*]
sequence:
  - action: ai_task.generate_data
    data:
      task_name: Letter
      entity_id: ai_task.blade_ai_task
      instructions: Read this letter. Give the sender, what to do in one short sentence, and the due date as YYYY-MM-DD if there is one.
      structure:
        sender: {selector: {text: {}}, required: true}
        action: {selector: {text: {}}, required: true}
        due: {selector: {text: {}}}
      attachments:
        - "{{ letter }}"
    response_variable: letter_data
  - action: todo.add_item
    target:
      entity_id: todo.tasks
    data:
      item: "{{ letter_data.data.sender }}: {{ letter_data.data.action }}"
```

**Recipe: read a meter**

```yaml
- action: blade.ocr
  data:
    camera_entity_id: camera.water_meter
    instructions: Give only the number shown on the counter.
  response_variable: reading
```

## Anticipate

`blade.forecast` predicts the next hours of any sensor that keeps long-term statistics, with a likely range.

**Ideas**

- **Greenest hour of the day.** Forecast the carbon intensity of your grid and suggest when to start the washing machine.
- **Appliance watchdog.** Compare a freezer's or a heat pump's power with its forecast; if reality leaves the likely range, something is wrong.
- **Pre-heat or pre-cool.** Forecast indoor temperature and start heating before it drops below comfort.
- **Solar and battery.** Forecast production or consumption to decide when to charge or discharge.

**Recipe: greenest hour**

```yaml
- action: blade.forecast
  data:
    entity_id: sensor.grid_carbon_intensity
    history_hours: 336
    horizon_hours: 16
  response_variable: forecast
- variables:
    best: "{{ forecast.forecast | sort(attribute='value') | first }}"
- action: notify.mobile_app_my_phone
  data:
    message: "Greenest time today: {{ as_local(as_datetime(best.datetime)).strftime('%H:%M') }}."
```

## Write and create

**Ideas**

- **An evening report.** Energy of the day, what is planned tomorrow and the weather, in four lines on your phone.
- **Birthday cards.** On a calendar birthday, write a personal message and generate a card with `ai_task.generate_image`.
- **Translate notices.** `blade.translate` for a message in a guest's language, or for a device manual.
- **Shopping list by aisle.** Reorder the shopping list by supermarket aisle before you leave.
- **Structured data from anything.** Turn free text (a delivery email, a note) into fields with a `structure`.

**Recipe: shopping list by aisle**

```yaml
- action: todo.get_items
  target:
    entity_id: todo.shopping_list
  data:
    status: needs_action
  response_variable: items
- action: ai_task.generate_data
  data:
    task_name: Shopping by aisle
    entity_id: ai_task.blade_ai_task
    instructions: >-
      Group these items by supermarket aisle, one line per aisle:
      {{ items['todo.shopping_list']['items'] | map(attribute='summary') | join(', ') }}
  response_variable: sorted
- action: notify.mobile_app_my_phone
  data:
    message: "{{ sorted.data }}"
```

## Talk to your home

**Ideas**

- **A ready-made voice assistant.** The Blade assistant created at setup combines the Blade agent, speech-to-text and streamed text-to-speech.
- **Your own personality.** Write instructions for a character; templates let the prompt know the time, the weather or the next train.
- **Bring your tools.** Paste Extended OpenAI Conversation functions (`native` execute_service, `script`, `template`) to give the agent searches, lists or forecasts of your own.
- **Spoken announcements.** A second agent without tools writes short announcements (end of a wash cycle, a door left open) that a TTS engine reads out.
- **Calculations.** With *Python code* on, the agent can compute what a language model should not guess: dates, conversions, averages.

**Recipe: a custom function the agent can call**

In the agent's **Custom functions** field:

```yaml
- spec:
    name: get_entity_state
    description: Read the state and attributes of one entity, read only.
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

## Keep spend in check

**Ideas**

- **A monthly cap.** Set it in the integration options; Blade calls pause when the estimate reaches it.
- **Early warning.** Notify at 80 % of the cap with the *Estimated cost this month* sensor.
- **Know what costs.** The cost sensors break spend down by service, so you see which agent or automation uses most.
- **Act on refusals.** Trigger on *Account status* leaving `ok` to be told at once when the balance is used up.

**Recipe: warn at 80 % of a 5 € cap**

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.blade_estimated_cost_this_month
    above: 4
actions:
  - action: notify.mobile_app_my_phone
    data:
      message: "Blade has used {{ states('sensor.blade_estimated_cost_this_month') }} € this month."
```

## Good practice

- Put the facts in `state` and the question in `instructions`; short, factual states give the best probabilities.
- Act only above a threshold (0.6 to 0.8), and keep the probability in the message so you can tune it.
- Prefer `ai_task.evaluate` to text generation when you only need a decision: it is faster and cheaper.
- Run frequent automations on small inputs; send long prompts only when needed.
- Keep web search off unless you need it: it is billed per search.
