C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
