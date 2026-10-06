from veronica.vision.uia_adapter import UIAutomationAdapter
adapter = UIAutomationAdapter()
print("=== RETEST: NOTEPAD AFTER REOPEN ===")
if adapter.is_available():
    windows = adapter.get_windows()
    notepad_win = None
    for w in windows:
        if w.process_name and 'notepad' in w.process_name.lower():
            notepad_win = w
            break
    if notepad_win:
        print("PID:", notepad_win.process_id, "bbox:", notepad_win.bounding_box, "visible:", notepad_win.is_visible)
        # Direct element scan for PID
        root = adapter._root_element
        uia = adapter._uia_modules["uia"]
        mod = adapter._uia_module
        found = None
        for i in range(30):
            try:
                desktop = root.FindAll(mod.TreeScope_Descendants, uia.CreateTrueCondition())
                elem = desktop.GetElement(i)
                pid = elem.GetCurrentPropertyValue(mod.UIA_ProcessIdPropertyId)
                if pid and int(pid) == notepad_win.process_id:
                    found = elem
                    break
            except:
                continue
        if found:
            print("Direct element PID:", found.GetCurrentPropertyValue(mod.UIA_ProcessIdPropertyId))
            print("Name:", found.GetCurrentPropertyValue(mod.UIA_NamePropertyId))
            rect = found.GetCurrentPropertyValue(mod.UIA_BoundingRectanglePropertyId)
            bbox = adapter._rect_to_bounding_box(rect) if rect else None
            print("Window bbox:", bbox)
            # Descendants
            desc = found.FindAll(mod.TreeScope_Descendants, uia.CreateTrueCondition())
            edit_found = False
            for j in range(min(desc.Length, 50)):
                try:
                    d = desc.GetElement(j)
                    d_type = d.GetCurrentPropertyValue(mod.UIA_ControlTypePropertyId)
                    d_str = adapter._control_type_id_to_string(d_type) if d_type else "?"
                    if d_str == "Edit":
                        d_name = d.GetCurrentPropertyValue(mod.UIA_NamePropertyId)
                        d_pid = d.GetCurrentPropertyValue(mod.UIA_ProcessIdPropertyId)
                        d_auto = d.GetCurrentPropertyValue(mod.UIA_AutomationIdPropertyId)
                        d_cname = d.GetCurrentPropertyValue(mod.UIA_ClassNamePropertyId)
                        d_rect = d.GetCurrentPropertyValue(mod.UIA_BoundingRectanglePropertyId)
                        d_bbox = adapter._rect_to_bounding_box(d_rect) if d_rect else None
                        d_off = d.GetCurrentPropertyValue(mod.UIA_IsOffscreenPropertyId) if hasattr(d, 'GetCurrentPropertyValue') else None
                        d_en = d.GetCurrentPropertyValue(mod.UIA_IsEnabledPropertyId)
                        d_foc = d.GetCurrentPropertyValue(mod.UIA_IsKeyboardFocusablePropertyId)
                        edit_found = True
                        print("EDIT -> Type=%s ID=%s Name=%r PID=%s Auto=%r Class=%r BBox=%s Off=%s En=%s Focus=%s" % (d_str, d_type, d_name, d_pid, d_auto, d_cname, d_bbox, d_off, d_en, d_foc))
                        # Candidate/correlation read-only
                        if d_bbox and d_bbox.get("width",0) > 0 and d_bbox.get("height",0) > 0:
                            from veronica.vision.visual_grounding import create_visual_grounding, VisualTargetCandidate, TargetReference
                            cand = VisualTargetCandidate(
                                semantic_name=d_name or "Notepad editor", control_type="Edit",
                                visual_bbox=(d_bbox["x"], d_bbox["y"], d_bbox["width"], d_bbox["height"]),
                                target_point=(d_bbox["x"]+d_bbox["width"]//2, d_bbox["y"]+d_bbox["height"]//2),
                                confidence=0.95, source="uia", process_id=int(d_pid) if d_pid else notepad_win.process_id,
                                window_title=notepad_win.title or "Untitled - Notepad", active_window=None,
                                detected_text=None, visual_description=None)
                            ref = TargetReference(
                                element_id=str(d_auto) if d_auto else "edit_" + str(int(d_pid) if d_pid else 12940),
                                target_name=d_name or "Notepad editor", control_type="Edit",
                                automation_id=str(d_auto) if d_auto else None,
                                class_name=d_cname or "Edit",
                                owning_process_id=int(d_pid) if d_pid else notepad_win.process_id,
                                owning_window_id=str(notepad_win.window_id) or "0",
                                owning_window_title=notepad_win.title or "Untitled - Notepad",
                                bounding_box=d_bbox,
                                target_point=(d_bbox["x"]+d_bbox["width"]//2, d_bbox["y"]+d_bbox["height"]//2))
                            res = create_visual_grounding().ground_target(cand, screen_state=None, references=[ref])
                            status = res.status.name if hasattr(res.status,"name") else str(res.status)
                            print("  Candidate: YES | Status:", status, "| Score:", res.score, "| VERIFIED:", status=="VERIFIED")
                        else:
                            print("  Edit bbox invalid; candidate NOT built.")
                except:
                    continue
            print("Descendants scanned (limit 50):", min(desc.Length, 50))
            print("Edit discovered:", edit_found)
            if not edit_found:
                print("STOP: Edit absent; no proposal; no auth.")
    else:
        print("Notepad not found in adapter.get_windows()")
else:
    print("UIA not available")
