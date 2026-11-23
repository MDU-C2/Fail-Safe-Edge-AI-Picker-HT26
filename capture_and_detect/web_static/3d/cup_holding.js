/* Where a cup is during a pick, from the finished step names (unit-tested).
 *
 * handover_plan: the left arm picks (grip_cup), the right arm grips it at the
 * handover (right_grip, both arms still) before the left opens (left_release),
 * and the right arm places it (right_release). The left-only plan lifts and
 * puts the cup back (replace_cup, left_release). A step counts once the robot
 * reported it DONE, so after a stop the cup stays with the arm that last held
 * it, as it does in reality. Returns the holder and the step that decided it,
 * whose time places the change on the arm playback timeline.
 */
export function cupHolder(motion) {
  const done = new Set(Array.isArray(motion?.completed_steps) ? motion.completed_steps : []);
  if (done.has("right_release")) return { holder: "released", step: "right_release" };
  if (done.has("right_grip")) return { holder: "right", step: "right_grip" };
  if (done.has("left_release")) return { holder: "released", step: "left_release" };
  if (done.has("grip_cup")) return { holder: "left", step: "grip_cup" };
  return { holder: "table", step: null };
}
