/* <pigeon/bmp.h> -- BMP images, in the screen's own pixels, at the size you
 * ask for (docs/bmp_plan.md).
 *
 *     unsigned *pixels = bmp_load("/etc/bmp/pigeon.bmp", DISP_W, DISP_H, BMP_CROP);
 *     if (pixels == NULL) print(bmp_strerror(bmp_error()));
 *     else memcpy((void *)DISP_BASE, pixels, DISP_W * DISP_H * 4u);
 *     free(pixels);
 *
 * What comes back is w x h words from malloc, row by row from the top left,
 * each 0xFFRRGGBB: the screen's own format, so a screenful copies straight
 * in. Free it when done. `unsigned`, the same bits as color_t, so this
 * header needn't include display.h and bring the display library along.
 *
 * Modes: BMP_CROP takes the middle w x h of the image at its own size,
 * BMP_CROP_TOP_LEFT its top-left corner, and where the image is smaller the
 * rest is black. BMP_STRETCH scales all of it to w x h with the nearest
 * pixel, not keeping the aspect.
 *
 * Files: 24- and 32-bit uncompressed BMPs, bottom-up or top-down, and 32-bit
 * ones saved with the usual bit fields; a 32-bit file's alpha is dropped.
 * Images up to 8,192 x 8,192, results up to 4,096 x 4,096.
 *
 * bmp_load and bmp_info read through the kernel (<pigeon/sys.h>). With no
 * kernel they fail with BMP_ENOKERNEL, and a program loads the file itself
 * -- fs_load_alloc -- and hands it to bmp_decode.
 */
#ifndef PIGEON_BMP_H
#define PIGEON_BMP_H

#define BMP_CROP          0
#define BMP_CROP_TOP_LEFT 1
#define BMP_STRETCH       2

/* bmp_error(): BMP_OK, one of these, or a file error from the kernel. */
#define BMP_OK          0
#define BMP_ENOKERNEL   (-300)  /* bmp_load or bmp_info with no kernel     */
#define BMP_ENOTBMP     (-301)  /* no BM, or a header too short to be one  */
#define BMP_EFORMAT     (-302)  /* not a 24- or 32-bit uncompressed BMP    */
#define BMP_ETRUNCATED  (-303)  /* the file ends before its pixels do      */
#define BMP_ETOOBIG     (-304)  /* over 8,192 pixels, or a result over 4,096 */
#define BMP_ENOMEM      (-305)  /* the heap can't hold the file or result  */
#define BMP_EARGS       (-306)  /* a width or height of 0, or no such mode */

unsigned *bmp_load  (char *path, unsigned w, unsigned h, int mode);  /* NULL on failure */
unsigned *bmp_decode(unsigned char *file, unsigned size,
                     unsigned w, unsigned h, int mode);              /* a file in memory */
int       bmp_info  (char *path, unsigned *w, unsigned *h);          /* its own size: BMP_OK or an error */
int       bmp_error (void);                                          /* why the last call failed */
char     *bmp_strerror(int err);

#endif
