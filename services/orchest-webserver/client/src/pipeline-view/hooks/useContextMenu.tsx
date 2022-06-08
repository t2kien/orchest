import { Position } from "@/types";
import Divider from "@mui/material/Divider";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import React from "react";

type MenuItemAction = {
  type: "item";
  title: string;
  disabled?: boolean;
  action: (props: {
    event: React.MouseEvent;
    itemId?: string;
    contextMenuPosition: Position;
  }) => void;
};

type MenuItemSeparator = {
  type: "separator";
};

export type MenuItem = MenuItemAction | MenuItemSeparator;

export function useContextMenu(
  items: MenuItem[],
  contextMenuState: [boolean, React.Dispatch<React.SetStateAction<boolean>>],
  itemId?: string
) {
  const [contextMenu, setContextMenu] = React.useState<{
    mouseX: number;
    mouseY: number;
  } | null>(null);

  const [contextMenuIsOpen, setContextMenuIsOpen] = contextMenuState;

  const handleClicked = (event: React.MouseEvent, item: MenuItemAction) => {
    if (contextMenu === null) {
      return;
    }

    item.action({
      event,
      itemId,
      contextMenuPosition: { x: contextMenu.mouseX, y: contextMenu.mouseY },
    });
    handleClose();
  };

  const handleClose = () => {
    setContextMenu(null);
    setContextMenuIsOpen(false);
  };

  const handleContextMenu = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (contextMenuIsOpen) {
      return;
    }

    setContextMenu(
      contextMenu === null
        ? {
            mouseX: event.clientX,
            mouseY: event.clientY,
          }
        : null
    );

    setContextMenuIsOpen(contextMenu === null);
  };

  const menu = (
    <Menu
      open={contextMenu !== null}
      onClose={handleClose}
      anchorReference="anchorPosition"
      anchorPosition={
        contextMenu !== null
          ? { top: contextMenu.mouseY, left: contextMenu.mouseX }
          : undefined
      }
    >
      {items.map((i) => {
        switch (i.type) {
          case "item":
            return (
              <MenuItem
                onClick={(e) => handleClicked(e, i)}
                disabled={i.disabled}
              >
                {i.title}
              </MenuItem>
            );
          case "separator":
            return <Divider />;
        }
      })}
    </Menu>
  );

  return { handleContextMenu, menu };
}
