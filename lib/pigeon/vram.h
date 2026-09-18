/* <pigeon/vram.h> -- video memory: the screen's mode, surfaces, and what
 * is on screen (CH_VRAM, docs/gac/).
 *
 *     unsigned w, h, offset;
 *     if (vram_present() && vram_mode(&w, &h, &offset)) { ... }
 *
 * The bytes are not reached through here. They are mapped above RAM, at
 * vram_aperture(), and a pixel is one store through a pointer into them.
 * This is the control side: modes, pieces of video memory, the page flip.
 *
 * NEVER take the aperture from a constant. It sits wherever this machine's
 * RAM ends, so a number baked in on a 128 MB machine is the middle of the
 * heap on a 1 GB one -- and a store there succeeds, silently. Ask
 * vram_aperture(). <pigeon/display.h> already does, and most programs never
 * need this header at all.
 *
 * On a machine without video memory, or a bare CPU, vram_present() is 0 and
 * everything else answers 0 or does nothing.
 */
#ifndef PIGEON_VRAM_H
#define PIGEON_VRAM_H

int      vram_present(void);                     /* 1: CH_VRAM answered its magic */
unsigned vram_size(void);                        /* bytes of video memory          */
unsigned vram_aperture(void);                    /* where it is mapped; see above  */
unsigned vram_generation(void);                  /* bumps on every mode change     */

int vram_mode(unsigned *w, unsigned *h, unsigned *offset);   /* the screen, surface 0 */
int vram_set_mode(unsigned w, unsigned h);       /* 0: not offered, or no room     */
unsigned vram_mode_count(void);
int vram_mode_at(unsigned i, unsigned *w, unsigned *h);      /* 0 past the end   */
unsigned vram_preferred(unsigned *w, unsigned *h);           /* -> the request count */

unsigned vram_alloc(unsigned w, unsigned h, unsigned *offset);  /* a handle, 0 if full */
int vram_free(unsigned handle);
int vram_scanout(unsigned handle);               /* the page flip: 0 is the screen */
int vram_scanout_ram(unsigned address);          /* the screen out of RAM again    */

/* Only the kernel wants these: surfaces allocated from now on belong to
 * `owner`, and everything owned by `owner` or deeper goes. */
int      vram_owner(unsigned owner);
unsigned vram_free_owned(unsigned owner);

#endif
