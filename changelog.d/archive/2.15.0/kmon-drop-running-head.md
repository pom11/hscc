kind: Changed
task: kmon-drop-running-head

- **`hscc kanban monitor`: no running heading.** The running table follows the
  token table directly; slots in use move onto the title line
  (`12/16 slots (≤2/profile)`, red when full) and the cap-full warning is a
  line of its own only while ready cards actually wait.
